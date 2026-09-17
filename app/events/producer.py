"""
A single, shared Kafka producer for the whole process.

Why a singleton: an AIOKafkaProducer owns TCP connections to every broker and a
background batching task. Creating one per request would open a connection per
request — the exact opposite of what a queue is for.
"""

from __future__ import annotations

import json
import logging

from aiokafka import AIOKafkaProducer

from app.core import metrics
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.core.config import settings
from app.events.topics import EventEnvelope

# Label for the one breaker this process has. A future second producer
# (unlikely per CLAUDE.md's "no microservices") would need its own label,
# not a reuse of this one, or the two would overwrite each other's gauge.
_BREAKER_COMPONENT = "relay_producer"

log = logging.getLogger(__name__)

_producer: AIOKafkaProducer | None = None

# One breaker for the process's one producer — see docs/adr/0007-observability.md
# Decision 2 for the state machine and why it beats a naive retry loop here.
_breaker = CircuitBreaker(
    failure_threshold=settings.circuit_breaker_failure_threshold,
    recovery_timeout_s=settings.circuit_breaker_recovery_timeout_s,
)


async def start_producer() -> None:
    global _producer
    if _producer is not None:
        return

    _producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        client_id=settings.kafka_client_id,
        value_serializer=lambda v: json.dumps(v).encode(),
        key_serializer=lambda k: k.encode() if k else None,
        **settings.kafka_security_kwargs(),

        # acks="all": the write is acknowledged only after every in-sync replica
        # has it. Slower than acks=1, but acks=1 loses data if the leader dies
        # before followers catch up. For money, never trade this away.
        acks="all",

        # Retries can reorder messages unless the broker deduplicates them.
        # Idempotence makes each retry safe and keeps per-partition order.
        enable_idempotence=True,

        # Wait up to 10ms to fill a batch. Costs 10ms of latency, buys a large
        # throughput win because far fewer requests hit the broker.
        linger_ms=10,
        compression_type="gzip",
    )
    await _producer.start()
    log.info("kafka producer started: %s", settings.kafka_bootstrap_servers)


async def stop_producer() -> None:
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None
        log.info("kafka producer stopped")


async def publish(topic: str, event: EventEnvelope) -> None:
    """
    Publish one event. Blocks until the broker acknowledges it.

    Note this is a *direct* publish. It is correct for now, but it is NOT safe
    once a DB write is involved: if the DB commit succeeds and this call fails,
    the world disagrees with your database. That is what the transactional
    outbox (next milestone) fixes.

    Guarded by the module's circuit breaker: while OPEN, this raises
    CircuitOpenError immediately instead of attempting the network call at
    all, so a dead broker fails in microseconds instead of after a full
    produce timeout — see ADR 0007 Decision 2.
    """
    if _producer is None:
        raise RuntimeError("producer not started")

    if not _breaker.allow_request():
        metrics.record_breaker_state(_BREAKER_COMPONENT, _breaker.state)
        raise CircuitOpenError("kafka producer circuit is open; broker considered unavailable")

    try:
        await _producer.send_and_wait(
            topic,
            key=event.aggregate_id,
            value=json.loads(event.model_dump_json()),
        )
    except Exception:
        _breaker.record_failure()
        raise
    else:
        _breaker.record_success()
    finally:
        metrics.record_breaker_state(_BREAKER_COMPONENT, _breaker.state)
