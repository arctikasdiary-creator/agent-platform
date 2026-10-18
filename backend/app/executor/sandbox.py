from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_MANAGED_LABEL = "agent.managed"
_TASK_LABEL = "agent.task_id"
_CREATED_LABEL = "agent.created_at"


class SandboxError(RuntimeError):
    pass


class SandboxTimeoutError(SandboxError):
    pass


@dataclass(slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


_locks: dict[str, asyncio.Lock] = {}


def _lock_for(task_id: str) -> asyncio.Lock:
    lock = _locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[task_id] = lock
    return lock


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


class TaskSandbox:
    def __init__(
        self,
        task_id: UUID | str,
        *,
        settings: Settings | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        self._task_id = str(task_id)
        self._settings = settings or get_settings()
        self._client = client
        self._container: Container | None = None

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def container_name(self) -> str:
        return f"agent-sandbox-{self._task_id}"

    @property
    def host_workspace(self) -> Path:
        return self._settings.workspaces_root / self._task_id

    def ensure_workspace(self) -> Path:
        self.host_workspace.mkdir(parents=True, exist_ok=True)
        return self.host_workspace

    def _docker(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def ensure_running(self) -> None:
        async with _lock_for(self._task_id):
            await asyncio.to_thread(self._ensure_running_sync)

    async def run(self, command: str, *, timeout_seconds: int) -> ExecResult:
        await self.ensure_running()
        container = self._container
        if container is None:
            raise SandboxError("sandbox container is not running")
        wrapped = ["timeout", "-k", "5", str(timeout_seconds), "bash", "-lc", command]

        def _exec() -> ExecResult:
            try:
                result = container.exec_run(
                    wrapped,
                    workdir=self._settings.sandbox_workdir,
                    demux=True,
                )
            except DockerException as exc:
                raise SandboxError(f"docker exec failed: {exc}") from exc
            stdout_raw, stderr_raw = result.output or (None, None)
            return ExecResult(
                exit_code=result.exit_code,
                stdout=_decode(stdout_raw),
                stderr=_decode(stderr_raw),
            )

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_exec), timeout=timeout_seconds + 15
            )
        except TimeoutError as exc:
            raise SandboxTimeoutError(
                f"command did not finish within {timeout_seconds}s: {command[:200]}"
            ) from exc

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop_sync)

    def _ensure_running_sync(self) -> None:
        try:
            client = self._docker()
            self.ensure_workspace()
            self._ensure_image(client)
            try:
                container = client.containers.get(self.container_name)
            except NotFound:
                self._container = self._create_container(client)
                return
            if container.status == "running":
                self._container = container
                return
            logger.info("removing stale sandbox container %s", self.container_name)
            try:
                container.remove(force=True)
            except DockerException:
                logger.exception("failed to remove stale sandbox container")
            self._container = self._create_container(client)
        except SandboxError:
            raise
        except DockerException as exc:
            raise SandboxError(f"sandbox unavailable: {exc}") from exc

    def _stop_sync(self) -> None:
        try:
            client = self._docker()
        except DockerException as exc:
            raise SandboxError(f"sandbox unavailable: {exc}") from exc
        container = self._container
        if container is None:
            try:
                container = client.containers.get(self.container_name)
            except NotFound:
                return
            except DockerException as exc:
                raise SandboxError(f"sandbox unavailable: {exc}") from exc
        try:
            container.remove(force=True)
        except NotFound:
            return
        except DockerException as exc:
            raise SandboxError(f"failed to remove sandbox container: {exc}") from exc
        finally:
            self._container = None

    def _ensure_image(self, client: docker.DockerClient) -> None:
        try:
            client.images.get(self._settings.sandbox_image)
        except ImageNotFound:
            logger.info("pulling sandbox image %s", self._settings.sandbox_image)
            try:
                client.images.pull(self._settings.sandbox_image)
            except DockerException as exc:
                raise SandboxError(
                    f"failed to pull sandbox image {self._settings.sandbox_image!r}: {exc}"
                ) from exc

    def _create_container(self, client: docker.DockerClient) -> Container:
        settings = self._settings
        try:
            return client.containers.run(
                image=settings.sandbox_image,
                name=self.container_name,
                command=["sleep", "infinity"],
                detach=True,
                working_dir=settings.sandbox_workdir,
                volumes={
                    str(self.host_workspace): {"bind": settings.sandbox_workdir, "mode": "rw"}
                },
                network_mode=settings.sandbox_network_mode,
                mem_limit=f"{settings.sandbox_memory_limit_mb}m",
                nano_cpus=int(settings.sandbox_cpu_limit * 1_000_000_000),
                pids_limit=settings.sandbox_pids_limit,
                read_only=True,
                tmpfs={"/tmp": f"size={settings.sandbox_tmpfs_size_mb}m"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                environment={
                    "HOME": "/tmp",
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                labels={
                    _MANAGED_LABEL: "true",
                    _TASK_LABEL: self._task_id,
                    _CREATED_LABEL: str(int(time.time())),
                },
            )
        except DockerException as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 409:
                return client.containers.get(self.container_name)
            raise SandboxError(f"failed to create sandbox container: {exc}") from exc


async def cleanup_containers(
    active_task_ids: set[str],
    *,
    older_than_seconds: int,
    client: docker.DockerClient | None = None,
) -> list[str]:
    return await asyncio.to_thread(
        _cleanup_containers_sync, active_task_ids, older_than_seconds, client
    )


def _cleanup_containers_sync(
    active_task_ids: set[str],
    older_than_seconds: int,
    client: docker.DockerClient | None,
) -> list[str]:
    try:
        docker_client = client or docker.from_env()
        containers = docker_client.containers.list(
            all=True, filters={"label": f"{_MANAGED_LABEL}=true"}
        )
    except DockerException as exc:
        logger.warning("sandbox cleanup skipped, docker unavailable: %s", exc)
        return []
    now = time.time()
    removed: list[str] = []
    for container in containers:
        labels = container.labels or {}
        task_id = labels.get(_TASK_LABEL)
        try:
            created_at = float(labels.get(_CREATED_LABEL, "0"))
        except ValueError:
            created_at = 0.0
        if task_id in active_task_ids:
            continue
        if created_at and now - created_at < older_than_seconds:
            continue
        try:
            container.remove(force=True)
            removed.append(container.name)
        except NotFound:
            removed.append(container.name)
        except DockerException as exc:
            logger.warning("failed to remove sandbox %s: %s", container.name, exc)
    return removed
