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
- Use between 1 and <MAX_STEPS> steps. Fewer steps is better; do not pad the plan.
- All file paths must be relative to the workspace root. Never use absolute paths, "..", or a leading "/".
- Never use curl, wget, pip, apt or git clone inside run_shell (no network). Use http_request for remote data.
- Never run firefox, chromium, xvfb, xvfb-run, node, npm or other third-party binaries in run_shell.
- run_shell steps must contain real commands that inspect or modify files. Never use echo/printf/true as placeholder "verification" or description steps; a check must exit non-zero when it fails.
- Never import third-party Python packages (selenium, playwright, requests, beautifulsoup4, PIL, numpy, pandas, etc.) in planned commands; use only the standard library.
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


def _sanitize_plan_steps(plan: Plan) -> Plan:
    kept: list[PlanStep] = []
    dropped = 0
    for step in plan.steps:
        if step.tool is ToolName.RUN_SHELL:
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
        kept.append(step)
    if dropped == 0:
        return plan
    if not kept:
        raise ValueError(
            "all planned steps were placeholder commands or rely on unavailable packages; "
            "produce a real plan using only the Python standard library and shell utilities"
        )
    return plan.model_copy(update={"steps": kept})


class Planner:
    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or get_llm_client()
        self._settings = get_settings()

    async def create_plan(self, *, title: str, prompt: str) -> Plan:
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
                plan = _sanitize_plan_steps(plan)
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
