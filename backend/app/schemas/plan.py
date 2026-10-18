from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import ToolName

PLAN_TOOLS: frozenset[ToolName] = frozenset(
    {
        ToolName.READ_FILE,
        ToolName.WRITE_FILE,
        ToolName.LIST_DIR,
        ToolName.RUN_SHELL,
        ToolName.HTTP_REQUEST,
    }
)

HTTP_METHODS: frozenset[str] = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})


def clean_relative_path(value: Any, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a non-empty string")
    path = value.strip().replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ValueError(f"path must be relative to the workspace root, got {value!r}")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"path must not contain '..': {value!r}")
    if not parts:
        if allow_root:
            return "."
        raise ValueError(f"path must point to a file inside the workspace, got {value!r}")
    return "/".join(parts)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tool: ToolName
    tool_input: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_tool_input(self) -> PlanStep:
        if self.tool not in PLAN_TOOLS:
            raise ValueError(
                f"tool {self.tool.value!r} is not allowed in a plan; "
                "task completion is automatic after the last step"
            )
        data = self.tool_input
        if self.tool is ToolName.READ_FILE:
            data["path"] = clean_relative_path(data.get("path"))
        elif self.tool is ToolName.WRITE_FILE:
            data["path"] = clean_relative_path(data.get("path"))
            if not isinstance(data.get("content"), str):
                raise ValueError("write_file requires 'content' to be a string")
        elif self.tool is ToolName.LIST_DIR:
            data["path"] = clean_relative_path(data.get("path", "."), allow_root=True)
        elif self.tool is ToolName.RUN_SHELL:
            command = data.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("run_shell requires a non-empty 'command' string")
            timeout = data.get("timeout_seconds")
            if timeout is not None:
                if not isinstance(timeout, (int, float)) or timeout <= 0:
                    raise ValueError("'timeout_seconds' must be a positive number")
                data["timeout_seconds"] = int(timeout)
        elif self.tool is ToolName.HTTP_REQUEST:
            method = str(data.get("method", "GET")).upper()
            if method not in HTTP_METHODS:
                raise ValueError(f"unsupported HTTP method {method!r}")
            url = data.get("url")
            scheme = urlparse(url).scheme if isinstance(url, str) else ""
            if scheme not in ("http", "https"):
                raise ValueError("http_request requires an absolute 'url' with http/https scheme")
            headers = data.get("headers") or {}
            if not isinstance(headers, dict):
                raise ValueError("'headers' must be an object with string values")
            body = data.get("body")
            if body is not None and not isinstance(body, str):
                raise ValueError("'body' must be a string")
            data["method"] = method
            data["headers"] = {str(key): str(value) for key, value in headers.items()}
        self.tool_input = data
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(default="", max_length=2000)
    steps: list[PlanStep] = Field(min_length=1, max_length=50)
