from __future__ import annotations

import enum
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import get_arq_pool
from app.db.models import Event, EventLevel

logger = logging.getLogger(__name__)


class EventKind(str, enum.Enum):
    TASK_CREATED = "task.created"
    TASK_PLANNING = "task.planning"
    TASK_PLANNED = "task.planned"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    STEP_STARTED = "step.started"
    STEP_OUTPUT = "step.output"
    STEP_SUCCEEDED = "step.succeeded"
    STEP_FAILED = "step.failed"
    STEP_SKIPPED = "step.skipped"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    ARTIFACT_CREATED = "artifact.created"


async def publish_event(
    session: AsyncSession,
    task_id: UUID,
    kind: EventKind | str,
    message: str,
    *,
    level: EventLevel = EventLevel.INFO,
    step_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    redis: Redis | None = None,
) -> Event:
    kind_value = kind.value if isinstance(kind, EventKind) else str(kind)
    event = Event(
        task_id=task_id,
        step_id=step_id,
        level=level,
        kind=kind_value,
        message=message,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    body: dict[str, Any] = {
        "id": event.id,
        "task_id": str(task_id),
        "step_id": str(step_id) if step_id is not None else None,
        "level": level.value,
        "kind": kind_value,
        "message": message,
        "payload": payload or {},
        "created_at": datetime.now(UTC).isoformat(),
    }
    client = redis if redis is not None else await get_arq_pool()
    try:
        await client.publish(
            get_settings().task_event_channel(task_id),
            json.dumps(body, default=str, ensure_ascii=False),
        )
    except Exception:
        logger.exception("failed to publish event %s for task %s to redis", kind_value, task_id)
    return event
