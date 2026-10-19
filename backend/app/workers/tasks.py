from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from arq import cron
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.events import EventKind, publish_event
from app.core.redis import close_arq_pool, get_redis_settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    EventLevel,
    Step,
    StepStatus,
    Task,
    TaskStatus,
    ToolName,
)
from app.db.session import SessionFactory, dispose_engine, init_models
from app.executor.artifacts import collect_artifacts
from app.executor.sandbox import TaskSandbox, cleanup_containers
from app.executor.tools import ToolContext, ToolError, execute_tool
from app.orchestrator.approvals import ApprovalError, decide_approval, is_expired
from app.orchestrator.planner import Planner
from app.orchestrator.scheduler import Scheduler

logger = logging.getLogger(__name__)

_SKIPPABLE_STATUSES = (StepStatus.PENDING, StepStatus.AWAITING_APPROVAL)

_ENVIRONMENT_ERROR_RE = re.compile(
    r"exit code 127|command not found|ModuleNotFoundError|No module named|Exec format error"
)


def _redact_tool_input(tool: ToolName, tool_input: dict[str, Any]) -> dict[str, Any]:
    if tool is ToolName.WRITE_FILE:
        data = dict(tool_input)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = f"<{len(content)} chars>"
        return data
    return tool_input


async def plan_task(ctx: dict[str, Any], task_id: str) -> None:
    settings = get_settings()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        logger.error("plan_task: invalid task id %r", task_id)
        return

    async with SessionFactory() as session:
        claim = await session.execute(
            update(Task)
            .where(Task.id == task_uuid, Task.status == TaskStatus.PENDING)
            .values(status=TaskStatus.PLANNING, started_at=func.now())
            .returning(Task.id)
        )
        claimed = claim.scalar_one_or_none()
        await session.commit()
        if claimed is None:
            logger.info("plan_task: task %s is not pending anymore, skipping", task_id)
            return

    async with SessionFactory() as session:
        task = await session.get(Task, task_uuid)
        if task is None:
            logger.error("plan_task: task %s disappeared", task_id)
            return
        await publish_event(
            session, task_uuid, EventKind.TASK_PLANNING, f"Planning task: {task.title}"
        )
        await session.commit()

        try:
            plan = await Planner().create_plan(title=task.title, prompt=task.prompt)
        except Exception as exc:
            logger.exception("plan_task: planning failed for task %s", task_id)
            task.status = TaskStatus.FAILED
            task.error = f"planning failed: {exc}"
            task.finished_at = datetime.now(UTC)
            await publish_event(
                session,
                task_uuid,
                EventKind.TASK_FAILED,
                f"Planning failed: {exc}",
                level=EventLevel.ERROR,
            )
            await session.commit()
            return

        task.plan = plan.model_dump(mode="json")
        task.status = TaskStatus.RUNNING
        for position, planned in enumerate(plan.steps):
            command = str(planned.tool_input.get("command", ""))
            requires_approval = (
                planned.tool is ToolName.RUN_SHELL
                and settings.requires_shell_approval(command)
            )
            task.steps.append(
                Step(
                    position=position,
                    title=planned.title,
                    description=planned.description,
                    tool=planned.tool,
                    tool_input=planned.tool_input,
                    requires_approval=requires_approval,
                )
            )
        await session.commit()

        steps = sorted(task.steps, key=lambda item: item.position)
        await publish_event(
            session,
            task_uuid,
            EventKind.TASK_PLANNED,
            f"Plan ready: {len(steps)} step(s)",
            payload={
                "summary": plan.summary,
                "steps": [
                    {
                        "id": str(step.id),
                        "position": step.position,
                        "title": step.title,
                        "tool": step.tool.value,
                        "requires_approval": step.requires_approval,
                    }
                    for step in steps
                ],
            },
        )
        await session.commit()

    if not steps:
        logger.warning("plan_task: task %s got an empty plan", task_id)
        return
    await Scheduler().enqueue_step(task_uuid, steps[0].id)


async def execute_step(ctx: dict[str, Any], task_id: str, step_id: str) -> None:
    settings = get_settings()
    scheduler = Scheduler()
    try:
        task_uuid = UUID(task_id)
        step_uuid = UUID(step_id)
    except ValueError:
        logger.error("execute_step: invalid ids %r %r", task_id, step_id)
        return

    async with SessionFactory() as session:
        claim = await session.execute(
            update(Step)
            .where(
                Step.id == step_uuid,
                Step.status.in_([StepStatus.PENDING, StepStatus.AWAITING_APPROVAL]),
            )
            .values(status=StepStatus.RUNNING, started_at=func.now())
            .returning(Step.id)
        )
        claimed = claim.scalar_one_or_none()
        await session.commit()
        if claimed is None:
            logger.info("execute_step: step %s is not claimable anymore, skipping", step_id)
            return

    async with SessionFactory() as session:
        step = await session.get(
            Step,
            step_uuid,
            options=[
                selectinload(Step.task).selectinload(Task.steps),
                selectinload(Step.approval),
            ],
        )
        if step is None or step.task is None:
            logger.error("execute_step: step %s or its task not found", step_id)
            return
        task = step.task

        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            step.status = StepStatus.SKIPPED
            step.error = f"task is {task.status.value}"
            step.finished_at = datetime.now(UTC)
            await session.commit()
            logger.info(
                "execute_step: task %s is %s, skipping step %s",
                task_uuid,
                task.status.value,
                step_id,
            )
            return

        if step.requires_approval:
            approval = step.approval
            if approval is None:
                command = str(step.tool_input.get("command", ""))
                approval = Approval(
                    task_id=task_uuid,
                    step_id=step.id,
                    command=command,
                    match_reason=settings.match_dangerous_command(command),
                    status=ApprovalStatus.PENDING,
                    expires_at=datetime.now(UTC)
                    + timedelta(seconds=settings.approval_ttl_seconds),
                )
                session.add(approval)
                step.status = StepStatus.AWAITING_APPROVAL
                task.status = TaskStatus.AWAITING_APPROVAL
                await session.flush()
                await publish_event(
                    session,
                    task_uuid,
                    EventKind.APPROVAL_REQUESTED,
                    f"Approval required before running: {command}",
                    step_id=step.id,
                    level=EventLevel.WARNING,
                    payload={
                        "approval_id": str(approval.id),
                        "command": command,
                        "match_reason": approval.match_reason,
                        "step_title": step.title,
                        "step_position": step.position,
                    },
                )
                await session.commit()
                return
            if approval.status is ApprovalStatus.PENDING:
                if is_expired(approval.expires_at, datetime.now(UTC)):
                    approval.status = ApprovalStatus.EXPIRED
                    approval.decided_at = datetime.now(UTC)
                else:
                    step.status = StepStatus.AWAITING_APPROVAL
                    task.status = TaskStatus.AWAITING_APPROVAL
                    await session.commit()
                    logger.info("execute_step: step %s still waiting for approval", step_id)
                    return
            if approval.status is not ApprovalStatus.APPROVED:
                error_text = f"approval was {approval.status.value}"
                step.status = StepStatus.FAILED
                step.error = error_text
                step.finished_at = datetime.now(UTC)
                await publish_event(
                    session,
                    task_uuid,
                    EventKind.STEP_FAILED,
                    f"Step {step.position + 1}: {error_text}",
                    step_id=step.id,
                    level=EventLevel.ERROR,
                )
                await session.commit()
                await _finalize_task(
                    session, task, error=f"step {step.position + 1}: {error_text}"
                )
                return
            task.status = TaskStatus.RUNNING

        step.attempts += 1
        workspace = settings.workspaces_root / str(task_uuid)
        workspace.mkdir(parents=True, exist_ok=True)
        sandbox = TaskSandbox(task_uuid, settings=settings)
        tool_ctx = ToolContext(
            task_id=task_uuid,
            step_id=step.id,
            workspace=workspace,
            sandbox=sandbox,
            settings=settings,
        )
        await publish_event(
            session,
            task_uuid,
            EventKind.STEP_STARTED,
            f"Step {step.position + 1}/{len(task.steps)}: {step.title}",
            step_id=step.id,
            payload={
                "tool": step.tool.value,
                "tool_input": _redact_tool_input(step.tool, step.tool_input),
            },
        )
        await session.commit()

        error_text: str | None = None
        tool_result = None
        try:
            tool_result = await execute_tool(tool_ctx, step.tool, step.tool_input)
        except ToolError as exc:
            error_text = str(exc)
        except Exception as exc:
            logger.exception("execute_step: unexpected error in step %s", step_id)
            error_text = f"internal error: {exc}"

        if (
            error_text is not None
            and step.tool is ToolName.RUN_SHELL
            and _ENVIRONMENT_ERROR_RE.search(error_text)
        ):
            step.status = StepStatus.SKIPPED
            step.error = error_text
            step.finished_at = datetime.now(UTC)
            await publish_event(
                session,
                task_uuid,
                EventKind.STEP_SKIPPED,
                f"Step {step.position + 1} skipped: required tool or module is not "
                "available in the sandbox",
                step_id=step.id,
                level=EventLevel.WARNING,
                payload={"error": error_text[:2000]},
            )
            await session.commit()
            enqueued = await scheduler.enqueue_next_step(session, task_uuid)
            if not enqueued:
                await _finalize_task(session, task, result=step.output)
            return

        if error_text is not None:
            if step.attempts < settings.step_max_attempts:
                step.status = StepStatus.PENDING
                step.error = error_text
                await publish_event(
                    session,
                    task_uuid,
                    EventKind.STEP_FAILED,
                    f"Step {step.position + 1} failed "
                    f"(attempt {step.attempts}/{settings.step_max_attempts}), retrying",
                    step_id=step.id,
                    level=EventLevel.WARNING,
                    payload={"will_retry": True, "error": error_text[:2000]},
                )
                await session.commit()
                await scheduler.enqueue_step(task_uuid, step.id, defer_seconds=5)
                return
            step.status = StepStatus.FAILED
            step.error = error_text
            step.finished_at = datetime.now(UTC)
            await publish_event(
                session,
                task_uuid,
                EventKind.STEP_FAILED,
                f"Step {step.position + 1} failed: {error_text[:500]}",
                step_id=step.id,
                level=EventLevel.ERROR,
                payload={"error": error_text[:2000]},
            )
            await session.commit()
            await _finalize_task(
                session,
                task,
                error=f"step {step.position + 1} ({step.title}) failed: {error_text}",
            )
            return

        step.output = tool_result.output
        step.status = StepStatus.SUCCEEDED
        step.error = None
        step.finished_at = datetime.now(UTC)
        await publish_event(
            session,
            task_uuid,
            EventKind.STEP_SUCCEEDED,
            f"Step {step.position + 1} succeeded: {step.title}",
            step_id=step.id,
            payload={"output_preview": tool_result.output[:1000]},
        )
        await session.commit()

        if tool_result.finished:
            await _finalize_task(session, task, result=tool_result.output)
            return

        enqueued = await scheduler.enqueue_next_step(session, task_uuid)
        if not enqueued:
            await _finalize_task(session, task, result=step.output)


async def _finalize_task(
    session: AsyncSession,
    task: Task,
    *,
    result: str | None = None,
    error: str | None = None,
) -> None:
    skipped = await session.execute(
        update(Step)
        .where(Step.task_id == task.id, Step.status.in_(list(_SKIPPABLE_STATUSES)))
        .values(status=StepStatus.SKIPPED, finished_at=func.now())
        .returning(Step.id)
    )
    skipped_count = len(skipped.all())
    artifacts = await collect_artifacts(session, task.id)
    artifact_names = [artifact.filename for artifact in artifacts]
    task.result = result[:4000] if result else None
    task.error = error
    task.status = TaskStatus.FAILED if error else TaskStatus.COMPLETED
    task.finished_at = datetime.now(UTC)
    if error:
        await publish_event(
            session,
            task.id,
            EventKind.TASK_FAILED,
            f"Task failed: {error[:500]}",
            level=EventLevel.ERROR,
            payload={"error": error[:4000], "artifacts": artifact_names},
        )
    else:
        await publish_event(
            session,
            task.id,
            EventKind.TASK_COMPLETED,
            f"Task completed: {task.title}",
            payload={"result": task.result, "artifacts": artifact_names},
        )
    await session.commit()
    logger.info(
        "task %s finalized as %s (artifacts=%d, skipped=%d)",
        task.id,
        task.status.value,
        len(artifact_names),
        skipped_count,
    )
    try:
        await TaskSandbox(task.id, settings=get_settings()).stop()
    except Exception:
        logger.exception("failed to remove sandbox for task %s", task.id)


async def _recover_stale_steps() -> None:
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.worker_job_timeout_seconds)
    async with SessionFactory() as session:
        result = await session.execute(
            update(Step)
            .where(Step.status == StepStatus.RUNNING, Step.started_at < cutoff)
            .values(status=StepStatus.PENDING, error="requeued after worker restart")
            .returning(Step.task_id, Step.id)
        )
        rows = result.all()
        await session.commit()
    if not rows:
        return
    scheduler = Scheduler()
    for task_id, step_id in rows:
        logger.warning("requeueing stale step %s of task %s", step_id, task_id)
        await scheduler.enqueue_step(task_id, step_id)


async def expire_approvals(ctx: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        result = await session.execute(
            select(Approval.id).where(
                Approval.status == ApprovalStatus.PENDING,
                Approval.expires_at.is_not(None),
                Approval.expires_at < now,
            )
        )
        approval_ids = list(result.scalars())
    if not approval_ids:
        return
    expired_count = 0
    for approval_id in approval_ids:
        try:
            await decide_approval(approval_id, approved=False, reason="approval TTL expired")
            expired_count += 1
        except ApprovalError as exc:
            logger.info("approval %s was not expired: %s", approval_id, exc)
    logger.info("expired %d approval(s)", expired_count)


_ORPHAN_DIR_MIN_AGE_SECONDS = 900


def _sweep_orphan_dirs(root: Path, known_task_ids: set[str]) -> int:
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for child in root.iterdir():
        if not child.is_dir() or child.name in known_task_ids:
            continue
        try:
            UUID(child.name)
        except ValueError:
            continue
        try:
            if now - child.stat().st_mtime < _ORPHAN_DIR_MIN_AGE_SECONDS:
                continue
            shutil.rmtree(child, ignore_errors=True)
        except OSError:
            logger.warning("failed to remove orphan directory %s", child, exc_info=True)
            continue
        removed += 1
    return removed


async def cleanup_orphan_files(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    async with SessionFactory() as session:
        result = await session.execute(select(Task.id))
        known = {str(task_id) for task_id in result.scalars()}
    removed = 0
    for root in (settings.workspaces_root, settings.artifacts_root):
        removed += await asyncio.to_thread(_sweep_orphan_dirs, root, known)
    if removed:
        logger.info("removed %d orphan task directory(ies)", removed)


async def cleanup_sandboxes(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    async with SessionFactory() as session:
        result = await session.execute(
            select(Task.id).where(
                Task.status.in_(
                    [
                        TaskStatus.PENDING,
                        TaskStatus.PLANNING,
                        TaskStatus.RUNNING,
                        TaskStatus.AWAITING_APPROVAL,
                    ]
                )
            )
        )
        active_task_ids = {str(task_id) for task_id in result.scalars()}
    removed = await cleanup_containers(
        active_task_ids, older_than_seconds=settings.sandbox_idle_timeout_seconds
    )
    if removed:
        logger.info("removed %d idle sandbox container(s)", len(removed))


async def on_startup(ctx: dict[str, Any]) -> None:
    await init_models()
    await _recover_stale_steps()


async def on_shutdown(ctx: dict[str, Any]) -> None:
    await close_arq_pool()
    await dispose_engine()


class WorkerSettings:
    functions = [plan_task, execute_step]
    cron_jobs = [
        cron(cleanup_sandboxes, minute=set(range(0, 60, 10))),
        cron(cleanup_orphan_files, minute=set(range(2, 60, 10))),
        cron(expire_approvals, minute=set(range(5, 60, 10))),
    ]
    redis_settings = get_redis_settings()
    max_jobs = get_settings().worker_max_jobs
    job_timeout = get_settings().worker_job_timeout_seconds
    keep_result = get_settings().worker_keep_result_seconds
    max_tries = 1
    on_startup = on_startup
    on_shutdown = on_shutdown
