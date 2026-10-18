from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import create_pubsub_client
from app.core.security import AuthError, create_access_token, decode_user_id, verify_password
from app.db.models import Approval, Artifact, Event, Task, TaskSource, User
from app.db.session import SessionFactory, get_session
from app.orchestrator.approvals import ApprovalError, decide_approval
from app.orchestrator.service import create_task
from app.schemas.api import (
    ApprovalDecision,
    ApprovalOut,
    EventOut,
    LoginRequest,
    TaskCreate,
    TaskOut,
    TaskSummary,
    TokenResponse,
    UserOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

WS_CODE_UNAUTHORIZED = 4401
WS_CODE_NOT_FOUND = 4404
SNAPSHOT_EVENT_LIMIT = 100
WS_POLL_TIMEOUT_SECONDS = 10.0


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: SessionDep,
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authorization required"
        )
    try:
        user_id = decode_user_id(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token") from exc
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user is inactive or missing"
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def _can_access(task: Task, user: User) -> bool:
    return user.is_admin or task.user_id == user.id


async def _get_accessible_task(session: AsyncSession, task_id: UUID, user: User) -> Task:
    task = await session.get(Task, task_id, populate_existing=True)
    if task is None or not _can_access(task, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return task


@router.post("/auth/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: SessionDep) -> TokenResponse:
    result = await session.execute(
        select(User).where(User.email == payload.email.strip().lower())
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user is disabled")
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=settings.jwt_access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/auth/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task_endpoint(
    payload: TaskCreate, user: CurrentUser, session: SessionDep
) -> Task:
    task = await create_task(
        session,
        prompt=payload.prompt,
        title=payload.title,
        user_id=user.id,
        source=TaskSource.WEB,
    )
    return await _get_accessible_task(session, task.id, user)


@router.get("/tasks", response_model=list[TaskSummary])
async def list_tasks(
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[Task]:
    query = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
    if not user.is_admin:
        query = query.where(Task.user_id == user.id)
    result = await session.execute(query)
    return list(result.scalars())


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task_endpoint(task_id: UUID, user: CurrentUser, session: SessionDep) -> Task:
    return await _get_accessible_task(session, task_id, user)


@router.get("/tasks/{task_id}/events", response_model=list[EventOut])
async def list_events(
    task_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    after_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
) -> list[Event]:
    await _get_accessible_task(session, task_id, user)
    result = await session.execute(
        select(Event)
        .where(Event.task_id == task_id, Event.id > after_id)
        .order_by(Event.id)
        .limit(limit)
    )
    return list(result.scalars())


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalOut)
async def decide_approval_endpoint(
    approval_id: UUID,
    payload: ApprovalDecision,
    user: CurrentUser,
    session: SessionDep,
) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    task = await session.get(Task, approval.task_id)
    if task is None or not _can_access(task, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    try:
        return await decide_approval(
            approval_id,
            approved=payload.approved,
            decided_by=user.id,
            reason=payload.reason,
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _resolve_artifact_path(artifact: Artifact) -> Path:
    settings = get_settings()
    root = settings.artifacts_root.resolve()
    path = Path(artifact.storage_path)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artifact file is not available"
        ) from exc
    if not resolved.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artifact file is not available"
        )
    return resolved


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: UUID, user: CurrentUser, session: SessionDep
) -> FileResponse:
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")
    task = await session.get(Task, artifact.task_id)
    if task is None or not _can_access(task, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")
    path = _resolve_artifact_path(artifact)
    return FileResponse(
        path,
        filename=artifact.filename.split("/")[-1],
        media_type=artifact.content_type or "application/octet-stream",
    )


async def _authenticate_ws(token: str | None) -> User:
    if not token:
        raise AuthError("missing token")
    user_id = decode_user_id(token)
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthError("user is inactive or missing")
    return user


async def _ws_reject(websocket: WebSocket, code: int, reason: str) -> None:
    with suppress(Exception):
        await websocket.send_json({"type": "error", "message": reason})
        await websocket.close(code=code, reason=reason)


async def _send_snapshot(websocket: WebSocket, task_id: UUID) -> None:
    async with SessionFactory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            await _ws_reject(websocket, WS_CODE_NOT_FOUND, "task not found")
            return
        result = await session.execute(
            select(Event)
            .where(Event.task_id == task_id)
            .order_by(Event.id.desc())
            .limit(SNAPSHOT_EVENT_LIMIT)
        )
        events = list(reversed(list(result.scalars())))
    await websocket.send_json(
        {
            "type": "snapshot",
            "task": TaskOut.model_validate(task).model_dump(mode="json"),
            "events": [EventOut.model_validate(item).model_dump(mode="json") for item in events],
        }
    )


@router.websocket("/ws/tasks/{task_id}")
async def task_events_websocket(
    websocket: WebSocket,
    task_id: UUID,
    token: str | None = None,
) -> None:
    await websocket.accept()
    try:
        user = await _authenticate_ws(token)
    except AuthError:
        await _ws_reject(websocket, WS_CODE_UNAUTHORIZED, "unauthorized")
        return

    async with SessionFactory() as session:
        task = await session.get(Task, task_id)
    if task is None or not _can_access(task, user):
        await _ws_reject(websocket, WS_CODE_NOT_FOUND, "task not found")
        return

    channel = get_settings().task_event_channel(task_id)
    client = create_pubsub_client()
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    try:
        await _send_snapshot(websocket, task_id)
        while True:
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=WS_POLL_TIMEOUT_SECONDS
                    ),
                    timeout=WS_POLL_TIMEOUT_SECONDS * 2,
                )
            except TimeoutError:
                message = None
            if message is None:
                await websocket.send_json({"type": "ping"})
                continue
            data = message.get("data")
            if not isinstance(data, str):
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            await websocket.send_json({"type": "event", "event": event})
    except Exception:
        logger.debug("websocket for task %s is closed", task_id, exc_info=True)
    finally:
        with suppress(Exception):
            await pubsub.unsubscribe(channel)
        with suppress(Exception):
            await pubsub.aclose()
        with suppress(Exception):
            await client.aclose()
