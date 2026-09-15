"""
Integration tests for app/services/retention.py — purging processed_events,
and the specific failure this module's docstring argues against: a dedup
record expiring before Kafka would have stopped redelivering the event it
guards.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import ProcessedEvent, ReadModelPayment
from app.events.consumer import handle
from app.events.topics import EventEnvelope
from app.services.retention import purge_processed_events
from tests.integration.conftest import authorize, unique

pytestmark = pytest.mark.integration


async def test_retention_deletes_only_rows_past_the_window():
    old_id, fresh_id = uuid.uuid4(), uuid.uuid4()
    group = unique("retention-group")

    async with SessionLocal() as session:
        async with session.begin():
            session.add(ProcessedEvent(event_id=old_id, consumer_group=group))
            session.add(ProcessedEvent(event_id=fresh_id, consumer_group=group))

    # Backdate one row past the window directly. processed_at is the only
    # thing purge_processed_events looks at, so this is equivalent to that
    # row genuinely having been processed retention_hours+1 ago.
    breached = datetime.now(UTC) - timedelta(hours=settings.processed_events_retention_hours + 1)
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                update(ProcessedEvent)
                .where(ProcessedEvent.event_id == old_id)
                .values(processed_at=breached)
            )

    async with SessionLocal() as session:
        async with session.begin():
            deleted = await purge_processed_events(session)

    async with SessionLocal() as session:
        remaining = (
            await session.execute(
                select(ProcessedEvent.event_id).where(ProcessedEvent.consumer_group == group)
            )
        ).scalars().all()

    assert deleted >= 1
    assert old_id not in remaining
    assert fresh_id in remaining


async def test_retention_run_twice_is_idempotent():
    """Repeated-invocation bias, same theme as the whole M3/M4 test suite:
    nothing about running an operational job twice should behave differently
    from running it once."""
    async with SessionLocal() as session:
        async with session.begin():
            await purge_processed_events(session)

    async with SessionLocal() as session:
        async with session.begin():
            second_run_deleted = await purge_processed_events(session)

    assert second_run_deleted == 0


async def test_reprocessing_after_dedup_record_expiry_is_still_safe(client, payer_payee):
    """
    The scenario this whole module exists to reason about: a processed_events
    row is gone (purged, or in this test, simulated by deleting it directly)
    and Kafka redelivers the event it used to guard. For this consumer — a
    pure upsert per ADR 0006 — reprocessing must leave the read model
    exactly as it was, not corrupt or double-apply it. This proves that
    claim rather than leaving it as an assertion in a docstring.
    """
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=3_300)

    event = EventEnvelope(
        event_type="payment.authorized",
        aggregate_id=payment["id"],
        payload={
            "payment_id": payment["id"],
            "status": "authorized",
            "amount_minor": payment["amount_minor"],
            "currency": payment["currency"],
            "payer_account_id": payer["id"],
            "payee_account_id": payee["id"],
        },
    )
    await handle(event)

    async with SessionLocal() as session:
        before = await session.get(ReadModelPayment, uuid.UUID(payment["id"]))
        before_snapshot = (before.status, before.amount_minor, before.last_event_id)

    # Simulate the dedup record's retention window having already expired.
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                delete(ProcessedEvent).where(ProcessedEvent.event_id == uuid.UUID(event.event_id))
            )

    # Simulate Kafka redelivering the same message after that.
    await handle(event)  # must not raise

    async with SessionLocal() as session:
        after = await session.get(ReadModelPayment, uuid.UUID(payment["id"]))
        after_snapshot = (after.status, after.amount_minor, after.last_event_id)

    assert before_snapshot == after_snapshot
