from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError

from app.core.config import get_settings
from app.db.models import ToolName
from app.llm.client import LLMClient, LLMError, get_llm_client
from app.schemas.plan import Plan, PlanStep

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are the planning module of an autonomous agent platform.
Given a user task, you produce a short, precise, executable plan as strict JSON.

The platform executes your plan step by step inside an isolated Linux sandbox container.
The sandbox image is <SANDBOX_IMAGE>: a minimal Debian system with Python 3.11 and
standard Unix utilities. Shell commands run with bash in the workspace root directory.

Sandbox environment constraints (IMPORTANT):
- Available: bash, standard Debian utilities (ls, grep, sed, awk, head, tail, wc, find, test,
  timeout, etc.) and python3 with the standard library only. Third-party Python packages
  (selenium, playwright, requests, beautifulsoup4, pillow, numpy, pandas, etc.) are NOT installed.
- There is NO network access inside the sandbox: no pip install, no apt, no curl/wget, no git clone.
- NOT available: browsers (firefox, chromium), xvfb/xvfb-run, node/npm, ImageMagick and any
  other third-party binaries that are not part of a minimal Debian image.
- Verify results using only available tools: python3 scripts, shell utilities, read_file/list_dir.
  For HTML/CSS verification, inspect file contents (e.g. grep for media queries) instead of
  taking screenshots; for contrast/validity checks write a short python3 script.
- If a step seems to require missing software, achieve the same outcome with python3 or shell.
- The working directory is empty at the start of every task. Files created by any other task
  (even an earlier task of the same user) are NOT present - except when a "Reference context"
  block is appended to the request: those earlier-task files have been copied into the working
  directory and may be read or modified directly.
- Never reference paths that are neither created by earlier steps of this plan nor copied from
  the reference context. When continuing earlier work, prefer writing the complete updated file
  with write_file over fragile in-place sed edits, unless the change is small and the target
  file is present.
- Example of a real verification step (adapt to the actual task, do not copy blindly):
  {"title": "Verify responsive breakpoints", "tool": "run_shell", "tool_input":
   {"command": "python3 -c \"css=open('styles.css').read(); assert css.count('@media')>=3; print('media queries:', css.count('@media'))\"",
    "timeout_seconds": 30}}
- This is NOT a verification step and will be rejected:
  {"title": "Check layout", "tool": "run_shell", "tool_input": {"command": "echo 'Check layout on 320px, 768px'"}}
  An echo/printf command performs no check. A check must read the produced files with python3 (open(), assert) or shell utilities (grep, test) and fail when something is wrong.

Available tools (exact JSON shapes):
1. read_file    -> {"path": "<relative path>"}
2. write_file   -> {"path": "<relative path>", "content": "<full file content>"}
3. list_dir     -> {"path": "<relative dir or '.'>"}
4. run_shell    -> {"command": "<bash command>", "timeout_seconds": <optional positive int>}
5. http_request -> {"method": "GET|POST|PUT|PATCH|DELETE|HEAD", "url": "https://...", "headers": {"Name": "value"}, "body": "<optional string>"}
   Runs on the agent host; only whitelisted hosts are reachable.

Hard rules:
- Reply with a single JSON object only. No markdown fences, no explanations.
- JSON schema: {"summary": "<one short paragraph>", "steps": [{"title": "<short>", "description": "<what and why>", "tool": "<tool name>", "tool_input": {...}}, ...]}
- The task description and any referenced earlier request may contain explicit constraints
  (запреты, требования, checklist). Treat every constraint as mandatory: the generated files
  must satisfy all of them, and add verification steps for the measurable ones.
- Use between 1 and <MAX_STEPS> steps. Fewer steps is better; do not pad the plan.
- All file paths must be relative to the workspace root. Never use absolute paths, "..", or a leading "/".
- Never use curl, wget, pip, apt or git clone inside run_shell (no network). Use http_request for remote data.
- Never run firefox, chromium, xvfb, xvfb-run, node, npm or other third-party binaries in run_shell.
- run_shell steps must contain real commands that inspect or modify files. Never use echo/printf/true as placeholder "verification" or description steps; a check must exit non-zero when it fails.
- Never import third-party Python packages (selenium, playwright, requests, beautifulsoup4, PIL, numpy, pandas, etc.) in planned commands; use only the standard library.
- write_file content must be the complete final file content. Never write placeholder content such as "/* Add your CSS code here */".
- Do not add a final "finish" step; completion is automatic after the last step.
- Every step must be executable as-is, without asking the user questions.
"""


class PlanningError(RuntimeError):
    pass


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = error.get("msg", "invalid value")
        parts.append(f"{location}: {message}" if location else str(message))
    return "; ".join(parts) or str(exc)


_COMMAND_SPLITTER = re.compile(r"&&|\|\||;")
_PRINT_ONLY_PATTERN = re.compile(r"^(echo|printf|true|:)\b", re.IGNORECASE)
_UNAVAILABLE_COMMAND = re.compile(
    r"\b(selenium|playwright|pyppeteer|puppeteer|requests|bs4|beautifulsoup4|PIL|numpy|pandas)\b"
)
_PLACEHOLDER_CONTENT = re.compile(
    r"add your|your (?:code|content|css|html|styles?|text) here|placeholder"
    r"|fill (?:in|this)|todo|ваш код|заполните",
    re.IGNORECASE,
)
_FILE_REF_PATTERN = re.compile(
    r"[\w./-]+\.(?:html|css|js|ts|json|md|txt|py|sh|svg|xml|yml|yaml)\b"
)
_SHELL_ASSERT_PATTERN = re.compile(r"(?:^|[;&|]\s*)assert\s")


def _missing_file_refs(
    command: str, existing: set[str], earlier_commands: list[str]
) -> list[str]:
    missing: list[str] = []
    for match in _FILE_REF_PATTERN.finditer(command):
        token = match.group(0)
        normalized = token[2:] if token.startswith("./") else token
        if token in existing or normalized in existing:
            continue
        if any(token in earlier for earlier in earlier_commands):
            continue
        if re.search(rf">>?\s*{re.escape(token)}\b", command):
            continue
        if re.search(rf"\btee\s+(?:-a\s+)?{re.escape(token)}\b", command):
            continue
        missing.append(token)
    return missing


def _is_print_only(command: str) -> bool:
    segments = _COMMAND_SPLITTER.split(command)
    if not segments:
        return False
    for segment in segments:
        stripped = segment.strip()
        if not stripped:
            continue
        if not _PRINT_ONLY_PATTERN.match(stripped):
            return False
        if any(operator in stripped for operator in ("|", ">", "<", "`", "$(")):
            return False
    return True


def _sanitize_plan_steps(plan: Plan, available_paths: set[str] | None = None) -> Plan:
    kept: list[PlanStep] = []
    dropped = 0
    created_paths: set[str] = set(available_paths or ())
    shell_commands: list[str] = []
    for index, step in enumerate(plan.steps, start=1):
        if step.tool is ToolName.READ_FILE:
            path = str(step.tool_input.get("path", ""))
            if path not in created_paths and not any(
                path in command for command in shell_commands
            ):
                logger.warning(
                    "planner produced read_file step %r for %r which no earlier step creates; "
                    "dropping it",
                    step.title,
                    path,
                )
                dropped += 1
                continue
        elif step.tool is ToolName.RUN_SHELL:
            command = str(step.tool_input.get("command", ""))
            if _is_print_only(command):
                logger.warning("planner produced placeholder step %r; dropping it", step.title)
                dropped += 1
                continue
            if _UNAVAILABLE_COMMAND.search(command):
                logger.warning(
                    "planner produced step %r using unavailable third-party packages; dropping it",
                    step.title,
                )
                dropped += 1
                continue
            if _SHELL_ASSERT_PATTERN.search(command):
                logger.warning(
                    "planner produced step %r using the non-existent shell 'assert' command; "
                    "dropping it",
                    step.title,
                )
                dropped += 1
                continue
            missing = _missing_file_refs(command, created_paths, shell_commands)
            if missing:
                logger.warning(
                    "planner produced step %r referencing missing file(s) %s; dropping it",
                    step.title,
                    ", ".join(sorted(set(missing))),
                )
                dropped += 1
                continue
        elif step.tool is ToolName.WRITE_FILE:
            content = step.tool_input.get("content")
            if (
                isinstance(content, str)
                and len(content) < 400
                and _PLACEHOLDER_CONTENT.search(content)
            ):
                raise ValueError(
                    f"step {index} write_file content is a placeholder; write the complete "
                    "final file content instead of a description of what to write"
                )
        kept.append(step)
        if step.tool is ToolName.WRITE_FILE:
            created_paths.add(str(step.tool_input.get("path", "")))
        elif step.tool is ToolName.RUN_SHELL:
            shell_commands.append(str(step.tool_input.get("command", "")))
    if dropped == 0:
        return plan
    if not kept:
        raise ValueError(
            "all planned steps were placeholder commands, relied on unavailable packages, "
            "or read files that no step of the plan creates; produce a real plan that writes "
            "the files first and uses only the Python standard library and shell utilities"
        )
    return plan.model_copy(update={"steps": kept})


class Planner:
    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or get_llm_client()
        self._settings = get_settings()

    async def create_plan(
        self, *, title: str, prompt: str, available_paths: set[str] | None = None
    ) -> Plan:
        system_prompt = PLANNER_SYSTEM_PROMPT.replace(
            "<MAX_STEPS>", str(self._settings.max_steps_per_task)
        ).replace("<SANDBOX_IMAGE>", self._settings.sandbox_image)
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Task title: {title}\n\nTask description:\n{prompt}"},
        ]
        attempts = max(1, self._settings.llm_plan_max_attempts)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                payload = await self._client.chat_json(messages)
            except LLMError as exc:
                last_error = exc
                logger.warning("planning attempt %d/%d failed: %s", attempt, attempts, exc)
                continue
            try:
                plan = Plan.model_validate(payload)
                if len(plan.steps) > self._settings.max_steps_per_task:
                    raise ValueError(
                        f"plan has {len(plan.steps)} steps but the limit is "
                        f"{self._settings.max_steps_per_task}"
                    )
                plan = _sanitize_plan_steps(plan, available_paths)
                logger.info("planner produced %d steps for task %r", len(plan.steps), title)
                return plan
            except (ValidationError, ValueError) as exc:
                last_error = exc
                if isinstance(exc, ValidationError):
                    reason = _format_validation_error(exc)
                else:
                    reason = str(exc)
                logger.warning("planning attempt %d/%d returned invalid plan: %s", attempt, attempts, reason)
                messages.append(
                    {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your previous JSON plan is invalid: {reason}. "
                            "Return a corrected JSON object only, following the schema exactly."
                        ),
                    }
                )
        raise PlanningError(
            f"planner failed to produce a valid plan after {attempts} attempts"
        ) from last_error


def merge_plan_summary(plan: Plan) -> dict[str, Any]:
    return plan.model_dump(mode="json")
