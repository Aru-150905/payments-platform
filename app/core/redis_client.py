"""
A single, shared Redis connection for the whole process.

Same reasoning as app/events/producer.py's singleton Kafka producer: a
redis.asyncio.Redis owns a connection pool, so building one per request would
mean opening a connection per request instead of reusing a pool sized once.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import settings

_redis: Redis | None = None


async def start_redis() -> None:
    global _redis
    if _redis is not None:
        return
    _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    await _redis.ping()


async def stop_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("redis client not started")
    return _redis
