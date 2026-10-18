from __future__ import annotations

import asyncio
from functools import lru_cache

from arq.connections import ArqRedis, RedisSettings, create_pool
from redis.asyncio import Redis

from app.core.config import get_settings


@lru_cache
def get_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


_pool: ArqRedis | None = None
_pool_lock = asyncio.Lock()


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await create_pool(get_redis_settings())
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def create_pubsub_client() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)
