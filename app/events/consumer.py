"""
The read-model consumer. Run with: python -m app.events.consumer

Projects payments.payment.v1 into read_model_payments (app/services/
read_model.py) under its own consumer group — see ADR 0006 for why the read
model is built this way and what it costs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import uuid

from aiokafka import AIOKafkaConsumer
from prometheus_client import start_http_server
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from app.core import metrics
from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import ProcessedEvent
from app.events.topics import PAYMENT_EVENTS, EventEnvelope
from app.services import read_model

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("read_model_consumer")


async def handle(event: EventEnvelope) -> None:
    """
    Process one event exactly once, from this consumer group's point of view.

    The dedup INSERT and the projection share one transaction, which this
    function owns start to finish — it opens SessionLocal() and
    session.begin() itself rather than being handed a session mid-transaction
    by a caller. That ownership rule is not a style preference: a prior bug
    in payments.py (see docs/adr/0005's postscript) came from a callee
    calling session.rollback() while nested inside a caller's session.begin(),
    which corrupted the caller's transaction. Nothing here ever calls
    rollback()/commit() directly for that reason — the `async with
    session.begin():` block does it, and only the function that opened it.
    """
    async with SessionLocal() as session:
        try:
            async with session.begin():
                session.add(
                    ProcessedEvent(
                        event_id=uuid.UUID(event.event_id),
                        consumer_group=settings.read_model_consumer_group,
                    )
                )
                # Flush now so the primary-key clash surfaces BEFORE the
                # projection runs, not after.
                await session.flush()

                await read_model.apply_event(session, event)

        except IntegrityError:
            # Already processed. Normal under at-least-once delivery — the
            # relay retries, Kafka redelivers after a rebalance, and both are
            # expected rather than exceptional. Safe to skip outright here
            # because the projection is idempotent-by-value (ADR 0006); a
            # consumer with non-idempotent side effects could not do this.
            log.debug("duplicate event %s skipped", event.event_id)


async def drain_once(consumer: AIOKafkaConsumer) -> int:
    """
    One getmany + handle-all + commit cycle. Pulled out of run() (the same
    split relay.py makes with its own drain_once()) so rebuild() and tests
    can drive exactly one poll cycle without standing up the infinite loop.

    Returns how many messages were seen in this cycle — 0 means "nothing new
    right now," which is what rebuild() uses to know it has caught up.
    """
    batch = await consumer.getmany(timeout_ms=1000, max_records=100)
    count = 0
    for _tp, messages in batch.items():
        for msg in messages:
            try:
                await handle(EventEnvelope(**msg.value))
            except Exception:
                # M3 routes this to the DLQ after bounded retries. For now,
                # log and move on rather than block the partition forever on
                # one poison message.
                log.exception("handler failed offset=%s", msg.offset)
            count += 1
    if batch:
        await consumer.commit()
    # Updated every cycle, not just when the batch was non-empty: an idle
    # topic still needs a fresh lag=0 reading, and a genuinely stuck consumer
    # (never reaching this line) is exactly the case this metric exists to
    # surface via a lag reading that stops updating at all — see ADR 0007
    # Decision 3.
    await _record_lag(consumer)
    return count


async def _record_lag(consumer: AIOKafkaConsumer) -> None:
    """
    lag = high-water mark (next offset the broker would assign) minus this
    consumer's own position (next offset it will fetch). enable_auto_commit
    is False and drain_once() commits right after processing, so `position`
    and the actually-committed offset agree by the time this runs — cheaper
    than a second round trip to ask the broker what was committed.
    """
    partitions = consumer.assignment()
    if not partitions:
        return
    end_offsets = await consumer.end_offsets(list(partitions))
    for tp in partitions:
        position = await consumer.position(tp)
        lag = max(0, end_offsets[tp] - position)
        metrics.kafka_consumer_lag.labels(
            topic=tp.topic,
            partition=str(tp.partition),
            group=settings.read_model_consumer_group,
        ).set(lag)


def _new_consumer() -> AIOKafkaConsumer:
    return AIOKafkaConsumer(
        PAYMENT_EVENTS,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.read_model_consumer_group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode()),
        # Cap how long one poll's worth of records may take before Kafka
        # assumes this consumer is dead and reassigns its partitions. Too low
        # and a slow batch triggers an endless rebalance loop.
        max_poll_interval_ms=300_000,
        **settings.kafka_security_kwargs(),
    )


async def run() -> None:
    # This process is never part of the FastAPI app, so it needs its own
    # /metrics HTTP endpoint — see ADR 0007 Decision 3's consequences.
    start_http_server(settings.consumer_metrics_port)
    consumer = _new_consumer()
    await consumer.start()
    log.info("read-model consumer started, group=%s", settings.read_model_consumer_group)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    try:
        while not stopping.is_set():
            # getmany()'s own timeout_ms inside drain_once() already paces
            # this; an idle topic just means an empty batch every second.
            await drain_once(consumer)
    finally:
        await consumer.stop()
        log.info("read-model consumer stopped")


async def rebuild() -> int:
    """
    Truncate the read model, forget this group's dedup history, and replay
    payments.payment.v1 from offset 0. Returns the number of events replayed.

    Do not run this while `make worker` (run(), above) is consuming under the
    same group at the same time. Kafka's consumer-group protocol would just
    split the topic's partitions between the two processes — the live
    consumer would keep committing progress on whatever it still owns while
    this has already wiped the table out from under it. See ADR 0006.
    """
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("TRUNCATE TABLE read_model_payments"))
            await session.execute(
                delete(ProcessedEvent).where(
                    ProcessedEvent.consumer_group == settings.read_model_consumer_group
                )
            )

    consumer = _new_consumer()
    await consumer.start()
    try:
        # start() joins the consumer group, but partition assignment can lag
        # the join by one heartbeat cycle. Seeking before it lands is a
        # silent no-op — there's nothing assigned yet to seek.
        while not consumer.assignment():
            await asyncio.sleep(0.05)
        await consumer.seek_to_beginning()

        total = 0
        while True:
            n = await drain_once(consumer)
            if n == 0:
                # An empty getmany() means caught up to the tail, which is
                # exactly "done replaying" for a one-shot rebuild — unlike
                # run(), which treats the same result as "idle, keep polling".
                break
            total += n
        return total
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(run())
