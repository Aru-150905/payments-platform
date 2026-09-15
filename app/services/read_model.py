"""
Pure projection logic for read_model_payments. See ADR 0006.

Pulled out of app/events/consumer.py for the same reason
app/services/ledger.py pulls validate_postings() out of post(): it holds the
rule that most needs testing (apply an event, get the right row), and it can
be tested with a real database but with no Kafka broker involved at all.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReadModelPayment
from app.events.topics import EventEnvelope

# The three payment lifecycle events app/services/payments.py emits. Payments
# never reach "refunded" or "failed" through the API today (no endpoint calls
# _transition() into either), so there is nothing yet that would emit those
# event types — this set exists to fail loudly if that changes without this
# module being updated too.
PROJECTED_EVENT_TYPES = {"payment.authorized", "payment.captured", "payment.voided"}


async def apply_event(session: AsyncSession, event: EventEnvelope) -> None:
    """
    Upsert read_model_payments from one event's payload.

    Deliberately a plain upsert-to-current-values, not a merge or an
    increment: applying the SAME event twice (a Kafka redelivery, a full
    rebuild replaying the whole topic) must produce the identical row both
    times. An INSERT ... ON CONFLICT DO UPDATE is how Postgres expresses
    "the whole row, freshly, every time" without a read-then-write race
    between two events for the same payment landing in the same batch.

    Ordering is Kafka's job, not this function's: the producer keys every
    message by aggregate_id (payment id), which guarantees all of one
    payment's events land in the same partition in send order. This function
    trusts that and applies whatever payload it's given as the new truth.
    """
    if event.event_type not in PROJECTED_EVENT_TYPES:
        return

    payload = event.payload
    stmt = pg_insert(ReadModelPayment).values(
        payment_id=uuid.UUID(payload["payment_id"]),
        status=payload["status"],
        amount_minor=payload["amount_minor"],
        currency=payload["currency"],
        payer_account_id=uuid.UUID(payload["payer_account_id"]),
        payee_account_id=uuid.UUID(payload["payee_account_id"]),
        last_event_id=uuid.UUID(event.event_id),
        last_event_type=event.event_type,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ReadModelPayment.payment_id],
        set_={
            "status": stmt.excluded.status,
            "amount_minor": stmt.excluded.amount_minor,
            "currency": stmt.excluded.currency,
            "payer_account_id": stmt.excluded.payer_account_id,
            "payee_account_id": stmt.excluded.payee_account_id,
            "last_event_id": stmt.excluded.last_event_id,
            "last_event_type": stmt.excluded.last_event_type,
            # ORM-level onupdate=func.now() on the model only fires for
            # ORM UPDATEs; this is a Core statement, so it's set explicitly.
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
