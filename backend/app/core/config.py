from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_DANGEROUS_SHELL_PATTERNS: tuple[str, ...] = (
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\b",
    r"\bsudo\b",
    r"\bsu\s+-",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\s+if=",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r"\bkill(all)?\s+-9\b",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\bchmod\s+-R\s+777\b",
    r"\bchown\s+-R\b",
    r"\b(curl|wget)\b[^|]{0,200}\|\s*(ba|z|da)?sh\b",
    r"\bapt(-get)?\s+(install|remove|purge|upgrade|dist-upgrade)\b",
    r"\bmount\b|\bumount\b",
    r"\biptables\b|\bnft\b",
    r">\s*/dev/(sd|nvme|vd)",
    r"/(etc/(passwd|shadow|sudoers))\b",
    r"\bnc\s+-[a-z]*l",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../deploy/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "agent-platform"
    environment: Literal["development", "staging", "production"] = "production"
    debug: bool = False
    secret_key: str = "change-me"
    api_base_url: str = "http://localhost:8000"
    web_base_url: str = "http://localhost:3000"

    database_url: str = "postgresql+asyncpg://agent:agent@postgres:5432/agent_platform"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    redis_url: str = "redis://redis:6379/0"
    event_channel_prefix: str = "agent.events"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.moonshot.ai/v1"
    llm_model: str = "kimi-k2.7-code"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 3
    llm_plan_max_attempts: int = 3

    telegram_bot_token: str = ""
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_admin_chat_id: int | None = None
    telegram_poll_timeout_seconds: int = 30
    telegram_proxy: str | None = None
    telegram_api_base_url: str | None = None

    jwt_secret_key: str = "change-me-jwt"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440
    web_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    workspaces_root: Path = Path("/data/workspaces")
    artifacts_root: Path = Path("/data/artifacts")

    sandbox_image: str = "python:3.11-slim"
    sandbox_network_mode: str = "none"
    sandbox_workdir: str = "/workspace"
    sandbox_memory_limit_mb: int = 512
    sandbox_cpu_limit: float = 1.0
    sandbox_pids_limit: int = 256
    sandbox_tmpfs_size_mb: int = 64
    sandbox_idle_timeout_seconds: int = 1800

    shell_timeout_seconds: int = 120
    shell_max_output_bytes: int = 262_144
    shell_approval_mode: Literal["dangerous", "all", "never"] = "dangerous"
    dangerous_shell_patterns: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_DANGEROUS_SHELL_PATTERNS)
    )

    http_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "api.github.com",
            "raw.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
        ]
    )
    http_request_timeout_seconds: float = 30.0
    http_max_response_bytes: int = 1_048_576

    max_steps_per_task: int = 20
    step_max_attempts: int = 2
    approval_ttl_seconds: int = 1800
    artifact_max_bytes: int = 52_428_800

    worker_max_jobs: int = 4
    worker_job_timeout_seconds: int = 900
    worker_keep_result_seconds: int = 3600

    @field_validator(
        "telegram_allowed_user_ids",
        "web_allowed_origins",
        "http_allowed_hosts",
        "dangerous_shell_patterns",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("telegram_admin_chat_id", mode="before")
    @classmethod
    def _empty_int_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "bootstrap_admin_email", "bootstrap_admin_password", "telegram_proxy", mode="before"
    )
    @classmethod
    def _empty_str_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    def task_event_channel(self, task_id: str | UUID) -> str:
        return f"{self.event_channel_prefix}.task.{task_id}"

    def match_dangerous_command(self, command: str) -> str | None:
        for pattern in self.dangerous_shell_patterns:
            if re.search(pattern, command, flags=re.IGNORECASE):
                return pattern
        return None

    def requires_shell_approval(self, command: str) -> bool:
        if self.shell_approval_mode == "never":
            return False
        if self.shell_approval_mode == "all":
            return True
        return self.match_dangerous_command(command) is not None

    def ensure_directories(self) -> None:
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
