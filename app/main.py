from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.api.rate_limit import RateLimitMiddleware, new_limiter
from app.api.routes import router
from app.core.config import settings
from app.core.redis_client import get_redis, start_redis, stop_redis
from app.db.base import SessionLocal

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The API no longer holds a Kafka producer at all. It writes outbox rows;
    # the relay process owns publication. One less thing that can make an HTTP
    # request fail because a broker hiccupped.
    #
    # Redis IS held here, unlike Kafka — the rate limiter needs it inline on
    # every request, not via a separately-owned relay process.
    await start_redis()
    app.state.rate_limiter = new_limiter(get_redis())
    try:
        yield
    finally:
        await stop_redis()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
# Middleware runs for every request before routing, including ones that will
# 404 or fail auth — intentional, since an unauthenticated flood is exactly
# what this exists to cap. See app/api/rate_limit.py.
app.add_middleware(RateLimitMiddleware)
app.include_router(router)


@app.get("/health/live")
async def live():
    """Is the process alive? Restart it if not. Must never touch a dependency."""
    return {"status": "ok"}


@app.get("/health/ready")
async def ready():
    """
    Can it serve traffic? Checks the database only.

    Kafka is deliberately NOT checked here. If it were, a broker outage would
    fail readiness, the load balancer would pull every instance, and reads that
    do not need Kafka at all would go down too. The outbox is what lets the API
    keep accepting writes while Kafka is unavailable — a readiness probe that
    ignores that would throw the benefit away.
    """
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "checks": {"postgres": "ok"}}
    except Exception as exc:
        return {"status": "degraded", "checks": {"postgres": str(exc)}}
