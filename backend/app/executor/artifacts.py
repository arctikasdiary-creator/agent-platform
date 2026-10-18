from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import shutil
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Artifact

logger = logging.getLogger(__name__)


def _iter_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def collect_artifacts(
    session: AsyncSession, task_id: UUID, *, step_id: UUID | None = None
) -> list[Artifact]:
    settings = get_settings()
    source_root = settings.workspaces_root / str(task_id)
    target_root = settings.artifacts_root / str(task_id)

    files = await asyncio.to_thread(_iter_files, source_root)
    if not files:
        return []

    existing = await session.execute(
        select(Artifact.storage_path).where(Artifact.task_id == task_id)
    )
    known = set(existing.scalars())

    created: list[Artifact] = []
    for path in files:
        relative = path.relative_to(source_root).as_posix()
        storage_path = f"{task_id}/{relative}"
        if storage_path in known:
            continue
        size = await asyncio.to_thread(path.stat().st_size)
        if size > settings.artifact_max_bytes:
            logger.warning(
                "artifact %s of task %s is %d bytes, exceeds the limit, skipped",
                relative,
                task_id,
                size,
            )
            continue
        digest = await asyncio.to_thread(_sha256, path)

        def _copy(source: Path = path, relative_path: str = relative) -> None:
            destination = target_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        await asyncio.to_thread(_copy)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        artifact = Artifact(
            task_id=task_id,
            step_id=step_id,
            filename=relative,
            storage_path=storage_path,
            content_type=content_type,
            size_bytes=size,
            sha256=digest,
        )
        session.add(artifact)
        created.append(artifact)
    if created:
        await session.flush()
        logger.info("collected %d artifact(s) for task %s", len(created), task_id)
    return created
