"""
Prometheus metrics. See docs/adr/0007-observability.md Decision 3 for why
these four and why kafka_consumer_lag is the one that matters most.

Three separate processes (API, relay, read-model consumer) each carry their
own copy of this module's default registry and expose their own /metrics —
see app/main.py, app/events/relay.py, app/events/consumer.py. A metric only
has real values in the process that actually produces it: the API never
touches Kafka directly (ADR 0006), so kafka_circuit_breaker_state only means
something scraped from the relay, and kafka_consumer_lag only from the
consumer.
"""

from __future__ import annotations

import time

from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.circuit_breaker import CircuitState

http_requests_total = Counter(
    "http_requests_total",
    "HTTP requests, by method, route template, and status code.",
    ["method", "route", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, by method and route template.",
    ["method", "route"],
)

kafka_circuit_breaker_state = Gauge(
    "kafka_circuit_breaker_state",
    "0=closed, 1=half_open, 2=open — the relay's Kafka producer breaker.",
    ["component"],
)

kafka_consumer_lag = Gauge(
    "kafka_consumer_lag",
    "Offsets a consumer group is behind the topic's current high-water mark.",
    ["topic", "partition", "group"],
)

_BREAKER_STATE_VALUE = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}


def record_breaker_state(component: str, state: CircuitState) -> None:
    kafka_circuit_breaker_state.labels(component=component).set(_BREAKER_STATE_VALUE[state])


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Registered as the outermost middleware in app/main.py (added last, so
    Starlette wraps it around everything else, including RateLimitMiddleware)
    specifically so it times and counts EVERY response this process sends —
    a 429 from the rate limiter and a 401 from auth are both real traffic,
    not noise to exclude.

    `request.scope["route"]` is only populated once Starlette's router
    resolves a match, which happens inside call_next(). For a 429 the
    request never reaches the router at all, so `route` stays "unmatched" —
    correct, since there genuinely was no route decision to label it with.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_s = time.perf_counter() - start

        route = request.scope.get("route")
        route_label = route.path if route is not None else "unmatched"

        http_requests_total.labels(
            method=request.method, route=route_label, status=str(response.status_code)
        ).inc()
        http_request_duration_seconds.labels(method=request.method, route=route_label).observe(
            duration_s
        )
        return response
