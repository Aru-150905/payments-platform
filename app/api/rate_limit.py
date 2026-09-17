"""
Redis-backed sliding-window rate limiting.

See docs/adr/0007-observability.md Decision 1 for why sliding window over
fixed window or token bucket. The boundary arithmetic (window_cutoff,
count_in_window) is split out as pure functions with no Redis dependency
specifically so it can be unit tested without infrastructure — the Lua
script below re-implements the identical rule inside Redis, where it has to
run atomically; see tests/test_rate_limit.py for both.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings

# Paths that must never be rate limited: a load balancer or orchestrator
# polling /health/* every few seconds would otherwise get itself throttled,
# and /metrics is scraped on its own fixed interval by Prometheus, not by a
# caller this limiter is meant to protect against.
EXEMPT_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})


# --------------------------------------------------------------------------
# pure boundary arithmetic — no Redis, no I/O
# --------------------------------------------------------------------------

def window_cutoff(now_ms: int, window_ms: int) -> int:
    """
    The window is `[now_ms - window_ms, now_ms]`, both ends inclusive: a
    request timestamped exactly at the cutoff is still counted (its window
    hasn't fully elapsed yet), one millisecond older is not. This exact rule
    is what the Lua script's ZREMRANGEBYSCORE has to mirror to stay correct.
    """
    return now_ms - window_ms


def count_in_window(timestamps: Iterable[int], *, now_ms: int, window_ms: int) -> int:
    cutoff = window_cutoff(now_ms, window_ms)
    return sum(1 for t in timestamps if t >= cutoff)


def is_allowed(count: int, *, limit: int) -> bool:
    return count < limit


# --------------------------------------------------------------------------
# Redis-backed limiter
# --------------------------------------------------------------------------

# Prune-then-count-then-maybe-add, all inside one EVAL so a concurrent
# request can never observe "under the limit" between this check and this
# write — see ADR 0007 Decision 1's atomicity paragraph.
_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

local cutoff = now_ms - window_ms
-- Exclusive upper bound '(' removes score < cutoff only, leaving score ==
-- cutoff — the same inclusive-at-the-cutoff rule count_in_window() applies
-- in Python.
redis.call('ZREMRANGEBYSCORE', key, '-inf', '(' .. cutoff)

local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now_ms, member)
    redis.call('PEXPIRE', key, window_ms)
    return {1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry_after_ms = window_ms
if oldest[2] ~= nil then
    retry_after_ms = (tonumber(oldest[2]) + window_ms) - now_ms
end
return {0, retry_after_ms}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_ms: int


class RedisSlidingWindowLimiter:
    def __init__(self, redis: Redis, *, window_ms: int, limit: int) -> None:
        self._window_ms = window_ms
        self._limit = limit
        self._script = redis.register_script(_SLIDING_WINDOW_SCRIPT)

    async def check(self, key: str) -> RateLimitResult:
        now_ms = int(time.time() * 1000)
        # Unique per call even when two requests land in the same
        # millisecond — ZADD would otherwise silently coalesce them into one
        # member and undercount.
        member = f"{now_ms}-{uuid.uuid4().hex}"
        allowed, retry_after_ms = await self._script(
            keys=[f"ratelimit:{key}"],
            args=[now_ms, self._window_ms, self._limit, member],
        )
        return RateLimitResult(allowed=bool(allowed), retry_after_ms=int(retry_after_ms))


# --------------------------------------------------------------------------
# the rejection response, and the middleware that produces it
# --------------------------------------------------------------------------

def rate_limited_response(*, retry_after_ms: int) -> JSONResponse:
    """
    Exact shape a client sees on 429: a JSON body with a machine-readable
    `error` code (not just a message a client would have to string-match)
    and `retry_after_ms` for precision, plus the standard `Retry-After`
    header in whole seconds — HTTP requires that header in seconds, and it
    is rounded UP so a client that obeys it literally never retries early.
    """
    retry_after_s = max(1, math.ceil(retry_after_ms / 1000))
    return JSONResponse(
        status_code=429,
        content={"error": "rate_limited", "retry_after_ms": retry_after_ms},
        headers={"Retry-After": str(retry_after_s)},
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Buckets by the caller's API key when present (so limiting is per-caller,
    matching ADR 0007 Decision 4), falling back to the client's address for
    requests that never send one — those are going to be rejected by auth
    anyway, but rejecting them still costs a Redis round trip and a DB
    connection attempt, which is exactly the load this limiter exists to cap.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        limiter: RedisSlidingWindowLimiter = request.app.state.rate_limiter
        api_key = request.headers.get("x-api-key")
        client_host = request.client.host if request.client else "unknown"
        bucket_key = api_key if api_key else f"ip:{client_host}"

        result = await limiter.check(bucket_key)
        if not result.allowed:
            return rate_limited_response(retry_after_ms=result.retry_after_ms)

        return await call_next(request)


def new_limiter(redis: Redis) -> RedisSlidingWindowLimiter:
    return RedisSlidingWindowLimiter(
        redis,
        window_ms=settings.rate_limit_window_ms,
        limit=settings.rate_limit_max_requests,
    )
