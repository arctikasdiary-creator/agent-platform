from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import selectinload

from app.core.events import EventKind, publish_event
from app.db.models import Approval, ApprovalStatus, EventLevel, Step, StepStatus, TaskStatus
from app.db.session import SessionFactory
from app.orchestrator.scheduler import Scheduler

logger = logging.getLogger(__name__)


class ApprovalError(Exception):
    """Raised when an approval cannot be decided."""


def is_expired(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < now


async def decide_approval(
    approval_id: UUID,
    *,
    approved: bool,
    decided_by: int | None = None,
    reason: str | None = None,
) -> Approval:
    async with SessionFactory() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise ApprovalError(f"approval {approval_id} not found")
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"approval is already {approval.status.value}")

        step = await session.get(Step, approval.step_id, options=[selectinload(Step.task)])
        if step is None or step.task is None:
            raise ApprovalError("approval is no longer attached to an existing step")
        task = step.task

        now = datetime.now(UTC)
        expired = is_expired(approval.expires_at, now)
        if expired:
            new_status = ApprovalStatus.EXPIRED
            approved = False
            reason = reason or "approval TTL expired"
        else:
            new_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED

        claim = await session.execute(
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == ApprovalStatus.PENDING)
            .values(
                status=new_status,
                decided_by=decided_by,
                decision_reason=reason,
                decided_at=now,
            )
            .returning(Approval.id)
        )
        if claim.scalar_one_or_none() is None:
            await session.rollback()
            raise ApprovalError("approval was decided concurrently, refresh and try again")

        approval.status = new_status
        approval.decided_by = decided_by
        approval.decision_reason = reason
        approval.decided_at = now

        step.status = StepStatus.PENDING
        step.error = None
        task.status = TaskStatus.RUNNING

        if approved:
            await publish_event(
                session,
                task.id,
                EventKind.APPROVAL_GRANTED,
                f"Approved: {approval.command}",
                step_id=step.id,
                payload={"approval_id": str(approval.id), "decided_by": decided_by},
            )
        else:
            await publish_event(
                session,
                task.id,
                EventKind.APPROVAL_REJECTED,
                f"Rejected: {approval.command}",
                step_id=step.id,
                level=EventLevel.WARNING,
                payload={
                    "approval_id": str(approval.id),
                    "decided_by": decided_by,
                    "reason": reason,
                    "expired": expired,
                },
            )
        await session.commit()

    try:
        await Scheduler().enqueue_step(task.id, step.id)
    except Exception:
        logger.exception("failed to enqueue step %s after approval decision", step.id)
    return approval
