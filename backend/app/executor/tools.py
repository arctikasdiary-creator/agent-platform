from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from uuid import UUID

import httpx

from app.core.config import Settings, get_settings
from app.db.models import ToolName
from app.executor.sandbox import SandboxError, TaskSandbox
from app.schemas.plan import clean_relative_path

logger = logging.getLogger(__name__)

_LIST_LIMIT = 500
_MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_TEXT_HINTS = ("json", "xml", "javascript", "text/", "yaml", "x-www-form-urlencoded")


class ToolError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolContext:
    task_id: UUID
    step_id: UUID
    workspace: Path
    sandbox: TaskSandbox
    settings: Settings


@dataclass(slots=True)
class ToolResult:
    output: str
    payload: dict[str, Any] | None = None
    finished: bool = False


def resolve_workspace_path(root: Path, relative: str) -> Path:
    cleaned = clean_relative_path(relative, allow_root=True)
    base = root.resolve()
    candidate = (base / cleaned).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        raise ToolError(f"path escapes the workspace: {relative!r}")
    return candidate


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _require_str(data: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ToolError(f"{key!r} must be a string")
    if not allow_empty and not value.strip():
        raise ToolError(f"{key!r} must be a non-empty string")
    return value


async def tool_read_file(ctx: ToolContext, path: str) -> ToolResult:
    target = resolve_workspace_path(ctx.workspace, path)
    if not await asyncio.to_thread(target.exists):
        raise ToolError(f"file not found: {path}")
    if await asyncio.to_thread(target.is_dir):
        raise ToolError(f"{path} is a directory, use list_dir instead")
    data = await asyncio.to_thread(target.read_bytes)
    limit = ctx.settings.shell_max_output_bytes
    truncated = len(data) > limit
    chunk = data[:limit]
    if b"\x00" in chunk:
        encoded = base64.b64encode(chunk).decode("ascii")
        output = f"[binary file, {len(data)} bytes, base64 of first {len(chunk)} bytes]\n{encoded}"
    else:
        output = chunk.decode("utf-8", errors="replace")
    if truncated:
        output += f"\n[truncated: file is {len(data)} bytes, showing first {len(chunk)}]"
    return ToolResult(output=output)


async def tool_write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    target = resolve_workspace_path(ctx.workspace, path)
    if await asyncio.to_thread(target.is_dir):
        raise ToolError(f"{path} is a directory, cannot write to it")

    def _write() -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = content.encode("utf-8")
        target.write_bytes(payload)
        return len(payload)

    written = await asyncio.to_thread(_write)
    return ToolResult(output=f"wrote {written} bytes to {path}")


async def tool_list_dir(ctx: ToolContext, path: str) -> ToolResult:
    target = resolve_workspace_path(ctx.workspace, path or ".")
    if not await asyncio.to_thread(target.exists):
        raise ToolError(f"directory not found: {path}")
    if not await asyncio.to_thread(target.is_dir):
        raise ToolError(f"{path} is not a directory")

    def _list() -> list[str]:
        entries: list[str] = []
        for entry in sorted(
            target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
        ):
            if entry.is_dir():
                entries.append(f"{entry.name}/")
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = -1
            entries.append(f"{entry.name} ({size} bytes)")
        return entries

    entries = await asyncio.to_thread(_list)
    shown = entries[:_LIST_LIMIT]
    output = "\n".join(shown) if shown else "(empty directory)"
    if len(entries) > _LIST_LIMIT:
        output += f"\n[truncated: {len(entries) - _LIST_LIMIT} more entries]"
    return ToolResult(output=output)


async def tool_run_shell(
    ctx: ToolContext, command: str, timeout_seconds: int | None = None
) -> ToolResult:
    if not command.strip():
        raise ToolError("run_shell requires a non-empty command")
    timeout = ctx.settings.shell_timeout_seconds
    if timeout_seconds is not None:
        timeout = max(1, min(int(timeout_seconds), ctx.settings.shell_timeout_seconds))
    try:
        result = await ctx.sandbox.run(command, timeout_seconds=timeout)
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc
    limit = ctx.settings.shell_max_output_bytes
    stdout, stdout_truncated = _truncate(result.stdout, limit)
    stderr, stderr_truncated = _truncate(result.stderr, limit)
    parts = [f"exit_code: {result.exit_code}"]
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if stdout_truncated or stderr_truncated:
        parts.append("[output truncated]")
    output = "\n".join(parts)
    if result.exit_code != 0:
        raise ToolError(f"command failed (exit code {result.exit_code}):\n{output}")
    return ToolResult(output=output)


def _host_allowed(host: str, allowed: Sequence[str]) -> bool:
    normalized = host.lower().rstrip(".")
    for entry in allowed:
        candidate = entry.lower().strip().rstrip(".")
        if not candidate:
            continue
        if candidate.startswith("*."):
            if normalized.endswith(candidate[1:]) and normalized != candidate[2:]:
                return True
        elif normalized == candidate:
            return True
    return False


def _ensure_url_allowed(url: str, allowed: Sequence[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("only http and https URLs are allowed")
    host = parsed.hostname or ""
    if not host:
        raise ToolError(f"cannot parse a host from URL {url!r}")
    if not _host_allowed(host, allowed):
        raise ToolError(f"host {host!r} is not in the HTTP_ALLOWED_HOSTS whitelist")


async def tool_http_request(ctx: ToolContext, data: Mapping[str, Any]) -> ToolResult:
    method = str(data.get("method", "GET")).upper()
    url = _require_str(data, "url")
    raw_headers = data.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise ToolError("'headers' must be an object with string values")
    headers = {str(key): str(value) for key, value in raw_headers.items()}
    body = data.get("body")
    if body is not None and not isinstance(body, str):
        raise ToolError("'body' must be a string")
    payload = body.encode("utf-8") if body else None
    current_url = url
    current_method = method
    timeout = httpx.Timeout(ctx.settings.http_request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response: httpx.Response | None = None
        for _ in range(_MAX_REDIRECTS + 1):
            _ensure_url_allowed(current_url, ctx.settings.http_allowed_hosts)
            try:
                response = await client.request(
                    current_method, current_url, headers=headers or None, content=payload
                )
            except httpx.HTTPError as exc:
                raise ToolError(f"request to {current_url} failed: {exc}") from exc
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_CODES and location:
                current_url = str(response.url.join(location))
                if response.status_code == 303:
                    current_method = "GET"
                    payload = None
                continue
            break
        if response is None:
            raise ToolError(f"too many redirects starting from {url}")
    raw = response.content[: ctx.settings.http_max_response_bytes]
    truncated = len(response.content) > len(raw)
    content_type = response.headers.get("content-type", "")
    if any(hint in content_type.lower() for hint in _TEXT_HINTS):
        body_text = raw.decode(response.encoding or "utf-8", errors="replace")
    else:
        body_text = base64.b64encode(raw).decode("ascii") + "\n[base64 encoded binary body]"
    parts = [f"status: {response.status_code}"]
    parts.append(f"content-type: {content_type or 'unknown'}")
    if truncated:
        parts.append(f"[body truncated to {ctx.settings.http_max_response_bytes} bytes]")
    parts.append("body:")
    parts.append(body_text)
    return ToolResult(output="\n".join(parts), payload={"status": response.status_code})


def tool_finish_task(data: Mapping[str, Any]) -> ToolResult:
    summary = data.get("summary") or data.get("result") or ""
    if not isinstance(summary, str):
        summary = str(summary)
    return ToolResult(output=summary.strip() or "task finished", finished=True)


async def execute_tool(
    ctx: ToolContext, tool: ToolName, tool_input: Mapping[str, Any]
) -> ToolResult:
    if tool is ToolName.READ_FILE:
        return await tool_read_file(ctx, _require_str(tool_input, "path"))
    if tool is ToolName.WRITE_FILE:
        return await tool_write_file(
            ctx,
            _require_str(tool_input, "path"),
            _require_str(tool_input, "content", allow_empty=True),
        )
    if tool is ToolName.LIST_DIR:
        return await tool_list_dir(ctx, str(tool_input.get("path") or "."))
    if tool is ToolName.RUN_SHELL:
        timeout_value = tool_input.get("timeout_seconds")
        timeout = int(timeout_value) if isinstance(timeout_value, (int, float)) else None
        return await tool_run_shell(ctx, _require_str(tool_input, "command"), timeout)
    if tool is ToolName.HTTP_REQUEST:
        return await tool_http_request(ctx, tool_input)
    if tool is ToolName.FINISH_TASK:
        return tool_finish_task(tool_input)
    raise ToolError(f"unknown tool: {tool}")
