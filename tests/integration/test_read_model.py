"""
Integration tests for the read-model projection and its rebuild command.
See ADR 0006 for the design this exercises.

Bias, per the project's own track record: four of five bugs found so far
were in error/retry/concurrency paths, not first-time happy paths. The
happy-path projection is one test here; everything else targets repeated
delivery, rebuild run twice, and rebuild's isolation from other consumer
groups' state.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import ProcessedEvent, ReadModelPayment
from app.events.consumer import handle, rebuild
from app.events.topics import EventEnvelope
from tests.integration.conftest import authorize, drain_pipeline, unique

pytestmark = pytest.mark.integration


def _event_for(
    payment: dict, payer: dict, payee: dict, *, event_type: str, status: str
) -> EventEnvelope:
    """
    Builds exactly the envelope app/services/payments.py's _emit() would have
    produced. Used to test the projection directly, without needing a real
    Kafka round trip for every test — drain_pipeline() (real Kafka) is
    reserved for the tests that specifically need to prove the wiring works.
    """
    return EventEnvelope(
        event_type=event_type,
        aggregate_id=payment["id"],
        payload={
            "payment_id": payment["id"],
            "status": status,
            "amount_minor": payment["amount_minor"],
            "currency": payment["currency"],
            "payer_account_id": payer["id"],
            "payee_account_id": payee["id"],
        },
    )


async def _read_model_row(payment_id: str) -> ReadModelPayment | None:
    async with SessionLocal() as session:
        return await session.get(ReadModelPayment, uuid.UUID(payment_id))


async def _read_model_snapshot() -> dict[uuid.UUID, tuple]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(ReadModelPayment))).scalars().all()
    return {
        r.payment_id: (r.status, r.amount_minor, r.currency, r.payer_account_id, r.payee_account_id)
        for r in rows
    }


async def test_projection_applies_authorize_then_capture_in_order(client, payer_payee):
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=2_500)

    authorized_event = _event_for(
        payment, payer, payee, event_type="payment.authorized", status="authorized"
    )
    await handle(authorized_event)
    row = await _read_model_row(payment["id"])
    assert row.status == "authorized"

    captured_event = _event_for(
        payment, payer, payee, event_type="payment.captured", status="captured"
    )
    await handle(captured_event)
    row = await _read_model_row(payment["id"])
    assert row.status == "captured"
    assert row.amount_minor == 2_500


async def test_repeated_delivery_of_the_same_event_is_a_no_op(client, payer_payee):
    """
    The dedup path (ProcessedEvent + IntegrityError) applying to the SAME
    event handed to handle() twice — a Kafka redelivery, not a rebuild.
    """
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=1_800)
    event = _event_for(payment, payer, payee, event_type="payment.captured", status="captured")

    await handle(event)
    first = await _read_model_row(payment["id"])

    await handle(event)  # same event_id — must be skipped, not re-applied or erroring
    second = await _read_model_row(payment["id"])

    assert (first.status, first.amount_minor, first.last_event_id) == (
        second.status, second.amount_minor, second.last_event_id,
    )


async def test_full_http_to_read_model_pipeline(client, payer_payee):
    """
    The one test that goes all the way through real HTTP -> outbox -> real
    relay -> real Kafka -> real consumer -> read model, proving the actual
    wiring works end to end, not just the projection function in isolation.
    """
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=7_700)
    captured = await client.post(f"/payments/{payment['id']}/capture")
    assert captured.status_code == 200, captured.text

    await drain_pipeline()

    row = await _read_model_row(payment["id"])
    assert row is not None, "payment never reached the read model"
    assert row.status == "captured"
    assert row.amount_minor == 7_700
    assert row.payer_account_id == uuid.UUID(payer["id"])
    assert row.payee_account_id == uuid.UUID(payee["id"])


async def test_rebuild_twice_produces_an_identical_read_model(client, payer_payee):
    """
    The required property: truncate-and-replay is deterministic. Two
    consecutive rebuilds against an unchanged topic must produce the exact
    same rows, not a doubled or partially-duplicated set.
    """
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=4_200)
    voided = await client.post(f"/payments/{payment['id']}/void")
    assert voided.status_code == 200, voided.text

    # Get this payment's events into Kafka at least once before rebuilding —
    # rebuild() replays the TOPIC, not the outbox, so it can't see an event
    # that was never relayed.
    await drain_pipeline()

    count_1 = await rebuild()
    snapshot_1 = await _read_model_snapshot()

    count_2 = await rebuild()
    snapshot_2 = await _read_model_snapshot()

    assert count_1 == count_2
    assert snapshot_1 == snapshot_2
    assert snapshot_1[uuid.UUID(payment["id"])][0] == "voided"


async def test_rebuild_removes_rows_the_current_topic_does_not_produce(client, payer_payee):
    """
    Proves TRUNCATE actually happens, not just re-projection over whatever
    was already there: a row with no corresponding event in the topic must
    not survive a rebuild.
    """
    ghost_id = uuid.uuid4()
    async with SessionLocal() as session:
        async with session.begin():
            session.add(
                ReadModelPayment(
                    payment_id=ghost_id,
                    status="captured",
                    amount_minor=1,
                    currency="INR",
                    payer_account_id=uuid.uuid4(),
                    payee_account_id=uuid.uuid4(),
                    last_event_id=uuid.uuid4(),
                    last_event_type="payment.captured",
                )
            )

    await rebuild()

    assert await _read_model_row(str(ghost_id)) is None


async def test_rebuild_does_not_disturb_another_consumer_groups_dedup_history(client, payer_payee):
    """
    The whole point of a dedicated consumer group: resetting THIS group's
    processed_events rows for a rebuild must never touch another group's.
    """
    other_group = unique("other-consumer-group")
    other_event_id = uuid.uuid4()
    async with SessionLocal() as session:
        async with session.begin():
            session.add(ProcessedEvent(event_id=other_event_id, consumer_group=other_group))

    await rebuild()

    async with SessionLocal() as session:
        survivor = await session.get(ProcessedEvent, (other_event_id, other_group))
    assert survivor is not None
