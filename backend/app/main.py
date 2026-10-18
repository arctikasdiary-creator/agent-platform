from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionFactory, dispose_engine, init_models

logger = logging.getLogger(__name__)

settings = get_settings()


async def _bootstrap_admin() -> None:
    email = settings.bootstrap_admin_email
    password = settings.bootstrap_admin_password
    if not email or not password:
        return
    normalized = email.strip().lower()
    async with SessionFactory() as session:
        result = await session.execute(select(User).where(User.email == normalized))
        user = result.scalar_one_or_none()
        if user is None:
            session.add(
                User(
                    email=normalized,
                    hashed_password=hash_password(password),
                    display_name="Admin",
                    is_admin=True,
                    is_active=True,
                )
            )
            await session.commit()
            logger.info("created bootstrap admin user %s", normalized)
            return
        changed = False
        if not user.is_admin:
            user.is_admin = True
            changed = True
        if user.hashed_password is None:
            user.hashed_password = hash_password(password)
            changed = True
        if changed:
            await session.commit()
            logger.info("updated bootstrap admin user %s", normalized)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings.ensure_directories()
    await init_models()
    await _bootstrap_admin()
    yield
    await dispose_engine()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.web_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
