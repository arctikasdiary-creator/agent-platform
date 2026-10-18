from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, publish_event
from app.db.models import Task, TaskSource, User
from app.orchestrator.scheduler import Scheduler

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 80


def derive_title(prompt: str, *, max_length: int = TITLE_MAX_LENGTH) -> str:
    text = " ".join(prompt.split())
    if not text:
        return "Untitled task"
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


async def create_task(
    session: AsyncSession,
    *,
    prompt: str,
    user_id: int | None = None,
    source: TaskSource = TaskSource.WEB,
    telegram_chat_id: int | None = None,
    title: str | None = None,
) -> Task:
    cleaned = prompt.strip()
    task = Task(
        user_id=user_id,
        title=(title.strip() if title else derive_title(cleaned))[:255],
        prompt=cleaned,
        source=source,
        telegram_chat_id=telegram_chat_id,
    )
    session.add(task)
    await session.flush()
    await publish_event(
        session,
        task.id,
        EventKind.TASK_CREATED,
        f"Task created: {task.title}",
        payload={"source": source.value, "prompt_preview": cleaned[:500]},
    )
    await session.commit()
    await Scheduler().enqueue_plan_task(task.id)
    return task


async def get_or_create_telegram_user(
    session: AsyncSession,
    telegram_id: int,
    display_name: str | None = None,
) -> User:
    normalized_name = display_name[:128] if display_name else None
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, display_name=normalized_name, is_active=True)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one()
        else:
            logger.info("registered telegram user %s", telegram_id)
        return user
    if normalized_name and user.display_name != normalized_name:
        user.display_name = normalized_name
        await session.commit()
    return user
