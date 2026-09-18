from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from app.api.rate_limit import RateLimitMiddleware, new_limiter
from app.api.routes import router
from app.api.trading_routes import router as trading_router
from app.core.config import settings
from app.core.metrics import MetricsMiddleware
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
# Added last so Starlette makes it the OUTERMOST layer, timing and counting
# literally everything this process responds with — see MetricsMiddleware's
# docstring for why that ordering is deliberate, not incidental.
app.add_middleware(MetricsMiddleware)
# CORS, outermost of all (added last): a browser's preflight OPTIONS request
# carries no X-API-Key and would otherwise be rejected by auth before ever
# reaching this middleware. allow_origins="*" is a deliberate local-dev-tool
# choice, not a production posture — frontend/ is a plain static page meant
# to be opened straight from disk or a throwaway `python -m http.server`,
# with no fixed origin to allow-list. There's no session cookie or browser
# credential this could leak (auth is a header the page attaches itself),
# which is what makes a wildcard origin acceptable here at all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(trading_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    The safety net CORS needs: Starlette's default behavior for an
    exception that reaches nobody's `except` clause is to let its own
    ServerErrorMiddleware — added automatically, OUTSIDE every middleware
    this file registers, CORSMiddleware included — build the 500 response.
    CORSMiddleware never sees that response to attach its headers to it, so
    a browser gets a same-origin-policy-shaped failure instead of a 500 it
    could read: fetch() reports the generic, undiagnosable "Failed to
    fetch" instead of surfacing any status code or body at all.
    Registering a handler for the base Exception runs it INSIDE the
    routing layer instead — which every middleware here, CORS included,
    already wraps — so the response this returns gets real CORS headers
    like any normal response would.

    Every specific exception this project already maps to a real status
    code (ledger.InsufficientFunds -> 422, IllegalTransition -> 409, ...)
    is still caught first, closer to where it happens, with a message that
    actually explains what went wrong — this only catches what's left: a
    genuine bug, not an expected rejection. The client still only sees
    "internal server error", never a stack trace; the real one is logged
    here for whoever reads this process's logs next.
    """
    logging.getLogger(__name__).exception(
        "unhandled exception on %s %s", request.method, request.url.path
    )
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


@app.get("/metrics")
async def metrics_endpoint():
    """
    Deliberately unauthenticated and rate-limit-exempt (app/api/rate_limit.py's
    EXEMPT_PATHS) — Prometheus scrapes this on its own fixed interval and
    can't be handed an API key any more than a load balancer's health check
    probe can.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
