"""
The outbox relay.

Run as its own process: `python -m app.events.relay`.

It polls for unpublished outbox rows, pushes them to Kafka, and marks them
published. Simple, but three details make it correct.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime

from prometheus_client import start_http_server
from sqlalchemy import select

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import OutboxEvent
from app.events.producer import publish, start_producer, stop_producer
from app.events.topics import EventEnvelope

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("relay")

BATCH_SIZE = 100
IDLE_SLEEP_S = 0.5


async def drain_once() -> int:
    """Publish one batch. Returns how many rows were sent."""
    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    # Ordering by id preserves the order events were produced
                    # in. Kafka only guarantees order per partition, so this is
                    # what keeps a payment's authorized-then-captured pair from
                    # being published backwards.
                    .order_by(OutboxEvent.id)
                    .limit(BATCH_SIZE)
                    # FOR UPDATE SKIP LOCKED lets you run several relay
                    # instances at once: each grabs a different batch instead
                    # of blocking on the same rows. Without SKIP LOCKED, a
                    # second relay is pure contention and adds no throughput.
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()

            if not rows:
                return 0

            for row in rows:
                envelope = EventEnvelope(
                    event_id=str(row.event_id),
                    event_type=row.event_type,
                    aggregate_id=row.aggregate_id,
                    occurred_at=row.created_at,
                    payload=row.payload,
                )
                # If this raises, the surrounding transaction rolls back and
                # published_at stays NULL, so the row is retried next tick.
                # Some events will therefore be published twice — which is
                # exactly why consumers dedup on event_id. At-least-once is a
                # design choice here, not an accident.
                await publish(row.topic, envelope)
                row.published_at = datetime.now(UTC)

            return len(rows)


async def run() -> None:
    # This process is never part of the FastAPI app (see this module's
    # docstring), so it needs its own /metrics HTTP endpoint rather than
    # riding on app/main.py's — see ADR 0007 Decision 3's consequences.
    start_http_server(settings.relay_metrics_port)
    await start_producer()

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    log.info("outbox relay started")
    try:
        while not stopping.is_set():
            try:
                sent = await drain_once()
            except Exception:
                log.exception("relay batch failed; backing off")
                await asyncio.sleep(2)
                continue

            if sent:
                log.info("published %d events", sent)
            else:
                # Nothing to do — sleep briefly instead of spinning the CPU.
                # A LISTEN/NOTIFY trigger would cut this latency to ~0 and is
                # the natural upgrade once polling shows up in your traces.
                await asyncio.sleep(IDLE_SLEEP_S)
    finally:
        await stop_producer()
        log.info("outbox relay stopped")


if __name__ == "__main__":
    asyncio.run(run())
