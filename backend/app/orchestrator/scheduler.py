from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_arq_pool
from app.db.models import Step, StepStatus

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, pool: ArqRedis | None = None) -> None:
        self._pool = pool

    async def _get_pool(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await get_arq_pool()
        return self._pool

    async def enqueue_plan_task(self, task_id: UUID) -> bool:
        pool = await self._get_pool()
        job = await pool.enqueue_job("plan_task", str(task_id), _job_id=f"plan:{task_id}")
        if job is None:
            logger.info("plan job for task %s is already queued", task_id)
            return False
        logger.info("enqueued plan_task for task %s", task_id)
        return True

    async def enqueue_step(
        self, task_id: UUID, step_id: UUID, *, defer_seconds: int | None = None
    ) -> bool:
        pool = await self._get_pool()
        enqueue_kwargs: dict[str, Any] = {}
        if defer_seconds:
            enqueue_kwargs["_defer_by"] = timedelta(seconds=defer_seconds)
        job = await pool.enqueue_job(
            "execute_step", str(task_id), str(step_id), **enqueue_kwargs
        )
        if job is None:
            logger.warning("failed to enqueue step %s of task %s", step_id, task_id)
            return False
        logger.info("enqueued execute_step %s for task %s", step_id, task_id)
        return True

    async def enqueue_next_step(self, session: AsyncSession, task_id: UUID) -> bool:
        result = await session.execute(
            select(Step.id)
            .where(Step.task_id == task_id, Step.status == StepStatus.PENDING)
            .order_by(Step.position)
            .limit(1)
        )
        step_id = result.scalar_one_or_none()
        if step_id is None:
            return False
        return await self.enqueue_step(task_id, step_id)
