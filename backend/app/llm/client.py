from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any, Sequence

from openai import APIError, AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


def extract_json_object(text: str) -> dict[str, Any]:
    candidates: list[str] = [text.strip()]
    candidates.extend(match.strip() for match in _FENCED_BLOCK_RE.findall(text))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("the response does not contain a valid JSON object")


def _is_unsupported_response_format(exc: APIError) -> bool:
    return exc.status_code == 400 and "response_format" in str(exc).lower()


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model = self._settings.llm_model
        self._client = AsyncOpenAI(
            api_key=self._settings.llm_api_key or "missing-api-key",
            base_url=self._settings.llm_base_url,
            timeout=self._settings.llm_timeout_seconds,
            max_retries=self._settings.llm_max_retries,
        )

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.close()

    async def chat(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._settings.llm_max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            completion = await self._request(payload)
        except APIError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        content = completion.choices[0].message.content if completion.choices else None
        if not content or not content.strip():
            raise LLMError("LLM returned an empty response")
        return content

    async def chat_json(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        attempts: int = 2,
        **kwargs: Any,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            raw = await self.chat(messages, json_mode=True, **kwargs)
            try:
                return extract_json_object(raw)
            except ValueError as exc:
                last_error = exc
                logger.warning(
                    "LLM returned invalid JSON (attempt %d/%d): %s", attempt, attempts, exc
                )
        raise LLMError(f"LLM did not return valid JSON after {attempts} attempts") from last_error

    async def _request(self, payload: dict[str, Any]) -> ChatCompletion:
        try:
            return await self._client.chat.completions.create(**payload)
        except APIError as exc:
            if "response_format" in payload and _is_unsupported_response_format(exc):
                logger.warning(
                    "model %s does not support response_format=json_object, retrying without it",
                    self._model,
                )
                payload = {key: value for key, value in payload.items() if key != "response_format"}
                return await self._client.chat.completions.create(**payload)
            raise


@lru_cache
def get_llm_client() -> LLMClient:
    return LLMClient()
