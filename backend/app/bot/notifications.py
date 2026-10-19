from __future__ import annotations

import asyncio
import html
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis
from sqlalchemy import select

from app.core.config import get_settings
from app.core.events import EventKind
from app.db.models import Artifact, Task, User
from app.db.session import SessionFactory

logger = logging.getLogger(__name__)

NOTIFY_KINDS = {
    EventKind.TASK_PLANNED.value,
    EventKind.APPROVAL_REQUESTED.value,
    EventKind.STEP_FAILED.value,
    EventKind.TASK_COMPLETED.value,
    EventKind.TASK_FAILED.value,
}


async def listen(bot: Bot, client: Redis) -> None:
    settings = get_settings()
    pattern = f"{settings.event_channel_prefix}.task.*"
    pubsub = client.pubsub()
    await pubsub.psubscribe(pattern)
    logger.info("notifications listener subscribed to %s", pattern)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=30.0
            )
            if message is None:
                continue
            data = message.get("data")
            if not isinstance(data, str):
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("kind") not in NOTIFY_KINDS:
                continue
            try:
                await _handle_event(bot, event)
            except Exception:
                logger.exception("notification handling failed for kind=%s", event.get("kind"))
    finally:
        with suppress(Exception):
            await pubsub.punsubscribe(pattern)
        with suppress(Exception):
            await pubsub.aclose()


async def _retry(call: Callable[[], Awaitable[Any]], *, attempts: int = 3) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await call()
            return
        except Exception:
            if attempt == attempts:
                raise
            logger.warning(
                "transient Telegram send error (attempt %d/%d), retrying", attempt, attempts
            )
            await asyncio.sleep(2 * attempt)


async def _handle_event(bot: Bot, event: dict[str, Any]) -> None:
    kind = str(event.get("kind") or "")
    payload = event.get("payload") or {}
    if kind == EventKind.STEP_FAILED.value and payload.get("will_retry"):
        return
    raw_task_id = event.get("task_id")
    if not raw_task_id:
        return
    try:
        task_id = UUID(str(raw_task_id))
    except ValueError:
        return

    async with SessionFactory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return
        chat_id = task.telegram_chat_id
        title = task.title
        if chat_id is None and task.user_id is not None:
            user = await session.get(User, task.user_id)
            if user is not None:
                chat_id = user.telegram_id
    if chat_id is None:
        return

    text = _render(kind, payload, event, title, task_id)
    if text is None:
        return

    keyboard = None
    approval_id = payload.get("approval_id")
    if kind == EventKind.APPROVAL_REQUESTED.value and approval_id:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Разрешить", callback_data=f"approve:{approval_id}"
                    ),
                    InlineKeyboardButton(
                        text="Отклонить", callback_data=f"reject:{approval_id}"
                    ),
                ]
            ]
        )

    try:
        await _retry(lambda: bot.send_message(chat_id, text, reply_markup=keyboard))
    except Exception:
        logger.exception("failed to send notification message for task %s", task_id)
    if kind == EventKind.TASK_COMPLETED.value:
        await _send_artifacts(bot, chat_id, task_id)


def _task_link(task_id: UUID) -> str:
    base = get_settings().web_base_url.rstrip("/")
    return f"{base}/tasks/{task_id}"


def _render(
    kind: str,
    payload: dict[str, Any],
    event: dict[str, Any],
    title: str,
    task_id: UUID,
) -> str | None:
    safe_title = html.escape(title)
    link = _task_link(task_id)
    if kind == EventKind.TASK_PLANNED.value:
        steps = payload.get("steps") or []
        lines = [f"<b>План готов</b>: {safe_title}", f"Шагов: {len(steps)}"]
        for index, step in enumerate(steps[:10], start=1):
            lines.append(f"{index}. {html.escape(str(step.get('title', '')))}")
        lines.append(f"\nДашборд: {link}")
        return "\n".join(lines)
    if kind == EventKind.APPROVAL_REQUESTED.value:
        command = html.escape(str(payload.get("command", "")))
        step_title = html.escape(str(payload.get("step_title", "")))
        lines = [
            f"<b>Требуется подтверждение</b>: {safe_title}",
            f"Шаг: {step_title}",
            f"Команда:\n<code>{command}</code>",
        ]
        match_reason = payload.get("match_reason")
        if match_reason:
            lines.append(f"Сработало правило: <code>{html.escape(str(match_reason))}</code>")
        return "\n".join(lines)
    if kind == EventKind.STEP_FAILED.value:
        message = html.escape(str(event.get("message", ""))[:600])
        return f"<b>Шаг не удался</b>: {safe_title}\n{message}\n\nДашборд: {link}"
    if kind == EventKind.TASK_COMPLETED.value:
        result = str(payload.get("result") or "")[:1200]
        lines = [f"<b>Задача выполнена</b>: {safe_title}"]
        if result:
            lines.append("")
            lines.append(html.escape(result))
        lines.append(f"\nДашборд: {link}")
        return "\n".join(lines)
    if kind == EventKind.TASK_FAILED.value:
        message = html.escape(str(event.get("message", ""))[:600])
        return f"<b>Задача не выполнена</b>: {safe_title}\n{message}\n\nДашборд: {link}"
    return None


async def _send_artifacts(bot: Bot, chat_id: int, task_id: UUID) -> None:
    settings = get_settings()
    async with SessionFactory() as session:
        result = await session.execute(
            select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.created_at)
        )
        artifacts = list(result.scalars())
    for artifact in artifacts:
        path = Path(artifact.storage_path)
        if not path.is_absolute():
            path = settings.artifacts_root / path
        if not path.is_file():
            logger.warning("artifact file is missing on disk: %s", path)
            continue
        try:
            safe_name = artifact.filename.split("/")[-1]
            await _retry(
                lambda path=path, safe_name=safe_name: bot.send_document(
                    chat_id, FSInputFile(path, filename=safe_name)
                )
            )
        except Exception:
            logger.exception("failed to send artifact %s of task %s", artifact.filename, task_id)
