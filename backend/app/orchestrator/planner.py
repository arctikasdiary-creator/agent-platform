from __future__ import annotations

import json
import logging
from typing import Any

from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError

from app.core.config import get_settings
from app.llm.client import LLMClient, LLMError, get_llm_client
from app.schemas.plan import Plan

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are the planning module of an autonomous agent platform.
Given a user task, you produce a short, precise, executable plan as strict JSON.

The platform executes your plan step by step inside an isolated Linux sandbox container.
The sandbox runs commands with bash, the working directory is the workspace root,
Python 3.11 is available. There is NO network access inside the sandbox.

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


class Planner:
    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or get_llm_client()
        self._settings = get_settings()

    async def create_plan(self, *, title: str, prompt: str) -> Plan:
        system_prompt = PLANNER_SYSTEM_PROMPT.replace(
            "<MAX_STEPS>", str(self._settings.max_steps_per_task)
        )
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
