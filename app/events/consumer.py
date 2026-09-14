"""
Consumer worker.  Run with: python -m app.events.consumer

Builds a read model from the event stream and demonstrates the two properties
that make a Kafka consumer safe: durable deduplication, and offsets committed
only after the work is durably done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import uuid

from aiokafka import AIOKafkaConsumer
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import ProcessedEvent
from app.events.topics import PAYMENT_EVENTS, EventEnvelope

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("consumer")


async def handle(event: EventEnvelope) -> None:
    """
    Process one event exactly once, from this consumer group's point of view.

    The dedup INSERT and the side effects share one database transaction. That
    is the crux: if they were separate, a crash between them would either
    replay the side effect or mark an event done that never ran.
    """
    async with SessionLocal() as session:
        try:
            async with session.begin():
                session.add(
                    ProcessedEvent(
                        event_id=uuid.UUID(event.event_id),
                        consumer_group=settings.kafka_consumer_group,
                    )
                )
                # Flush now so the primary-key clash surfaces BEFORE we do the
                # work, not after.
                await session.flush()

                # ---- side effects go here ----
                # M4 replaces this with the read-model projection.
                log.info(
                    "processing %s aggregate=%s payload=%s",
                    event.event_type, event.aggregate_id, event.payload,
                )

        except IntegrityError:
            # Already processed. Normal under at-least-once delivery — the
            # relay retries, Kafka redelivers after a rebalance, and both are
            # expected rather than exceptional.
            log.debug("duplicate event %s skipped", event.event_id)


async def run() -> None:
    consumer = AIOKafkaConsumer(
        PAYMENT_EVENTS,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_consumer_group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode()),
        # Cap how long one poll's worth of records may take before Kafka
        # assumes this consumer is dead and reassigns its partitions. Too low
        # and a slow batch triggers an endless rebalance loop.
        max_poll_interval_ms=300_000,
    )
    await consumer.start()
    log.info("consumer started, group=%s", settings.kafka_consumer_group)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    try:
        while not stopping.is_set():
            batch = await consumer.getmany(timeout_ms=1000, max_records=100)
            for _tp, messages in batch.items():
                for msg in messages:
                    try:
                        await handle(EventEnvelope(**msg.value))
                    except Exception:
                        # M3 routes this to the DLQ after bounded retries.
                        log.exception("handler failed offset=%s", msg.offset)
            if batch:
                # Commit AFTER the work. Auto-commit would commit on a timer,
                # possibly before handle() finished, silently losing events.
                await consumer.commit()
    finally:
        await consumer.stop()
        log.info("consumer stopped")


if __name__ == "__main__":
    asyncio.run(run())
