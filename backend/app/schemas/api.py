from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import (
    ApprovalStatus,
    EventLevel,
    StepStatus,
    TaskSource,
    TaskStatus,
    ToolName,
)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str | None = None
    display_name: str | None = None
    telegram_id: int | None = None
    is_active: bool
    is_admin: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class TaskCreate(BaseModel):
    prompt: str = Field(min_length=3, max_length=8000)
    title: str | None = Field(default=None, max_length=255)

    @field_validator("prompt")
    @classmethod
    def _clean_prompt(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("prompt must contain at least 3 characters")
        return value

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    step_id: UUID
    command: str
    match_reason: str | None = None
    status: ApprovalStatus
    decision_reason: str | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    created_at: datetime


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str | None = None
    created_at: datetime


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int
    title: str
    description: str
    tool: ToolName
    status: StepStatus
    requires_approval: bool
    attempts: int
    output: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    approval: ApprovalOut | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    prompt: str
    source: TaskSource
    status: TaskStatus
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    steps: list[StepOut] = Field(default_factory=list)
    artifacts: list[ArtifactOut] = Field(default_factory=list)


class TaskSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    status: TaskStatus
    source: TaskSource
    created_at: datetime
    finished_at: datetime | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: UUID
    step_id: UUID | None = None
    level: EventLevel
    kind: str
    message: str
    payload: dict[str, Any] | None = None
    created_at: datetime


class ApprovalDecision(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=1000)
