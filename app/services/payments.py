"""
Payment orchestration.

The flow, and why a payment is not just one transfer:

    authorize:  payer      -> clearing     (funds reserved, payee cannot spend)
    capture:    clearing   -> payee        (funds released)
    void:       clearing   -> payer        (reservation returned)
    refund:     payee      -> payer

A single payer->payee transfer would be simpler and wrong. Authorization and
capture are separated in time by everything from a card network's response to a
shipment leaving a warehouse, and during that window the money must be in
neither party's spendable balance. The clearing account is where it sits.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, OutboxEvent, Payment, PaymentStatus
from app.domain.state_machine import assert_can_transition
from app.events.topics import PAYMENT_EVENTS
from app.services import ledger
from app.services.ledger import Posting

CLEARING_OWNER = "platform"


class PaymentError(Exception):
    pass


class ConcurrentModification(PaymentError):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

async def _clearing_account(session: AsyncSession, currency: str) -> Account:
    account = (
        await session.execute(
            select(Account).where(
                Account.owner_id == CLEARING_OWNER,
                Account.currency == currency,
                Account.name == "clearing",
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise PaymentError(f"no clearing account for {currency} — run seed first")
    return account


def _emit(session: AsyncSession, payment: Payment, event_type: str) -> None:
    """
    Queue a domain event. Note this only session.add()s a row — it does not
    talk to Kafka. Publication is the relay's job precisely so that this can
    participate in the caller's database transaction.
    """
    session.add(
        OutboxEvent(
            topic=PAYMENT_EVENTS,
            event_type=event_type,
            aggregate_id=str(payment.id),
            payload={
                "payment_id": str(payment.id),
                "status": payment.status.value,
                "amount_minor": payment.amount_minor,
                "currency": payment.currency,
                "payer_account_id": str(payment.payer_account_id),
                "payee_account_id": str(payment.payee_account_id),
            },
        )
    )


async def _transition(
    session: AsyncSession, payment: Payment, target: PaymentStatus
) -> None:
    assert_can_transition(payment.status, target)
    payment.status = target
    payment.version += 1


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------

async def create_and_authorize(
    session: AsyncSession,
    *,
    idempotency_key: str,
    payer_account_id: uuid.UUID,
    payee_account_id: uuid.UUID,
    amount_minor: int,
    currency: str,
) -> Payment:
    """
    Create a payment and immediately reserve the funds.

    Idempotency is the client's key, enforced by a unique index. A retry after
    a network timeout must NOT create a second payment — the client genuinely
    cannot tell a lost response from a lost request, so the server has to be
    the one that makes retrying safe.
    """
    existing = (
        await session.execute(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    payment = Payment(
        idempotency_key=idempotency_key,
        payer_account_id=payer_account_id,
        payee_account_id=payee_account_id,
        amount_minor=amount_minor,
        currency=currency,
        status=PaymentStatus.INITIATED,
    )
    session.add(payment)

    try:
        await session.flush()
    except IntegrityError:
        # Lost the race against a concurrent identical request. The other one
        # won; read its result and return that.
        await session.rollback()
        return (
            await session.execute(
                select(Payment).where(Payment.idempotency_key == idempotency_key)
            )
        ).scalar_one()

    clearing = await _clearing_account(session, currency)

    await ledger.post(
        session,
        kind="payment.authorize",
        idempotency_key=f"auth:{payment.id}",
        postings=[
            Posting(payer_account_id, -amount_minor),   # credit the payer
            Posting(clearing.id, +amount_minor),        # debit clearing
        ],
    )

    await _transition(session, payment, PaymentStatus.AUTHORIZED)
    _emit(session, payment, "payment.authorized")
    return payment


async def capture(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    payment = await _locked(session, payment_id)

    # Check the state machine before touching the ledger. This is a cheap
    # in-memory check and must run before any database write: a repeated
    # capture would otherwise reach ledger.post() first and fail on
    # uq_ledger_tx_idempotency with a raw IntegrityError, which surfaces to
    # the client as a 500 instead of the 409 an illegal transition should be.
    assert_can_transition(payment.status, PaymentStatus.CAPTURED)

    clearing = await _clearing_account(session, payment.currency)

    await ledger.post(
        session,
        kind="payment.capture",
        idempotency_key=f"capture:{payment.id}",
        postings=[
            Posting(clearing.id, -payment.amount_minor),
            Posting(payment.payee_account_id, +payment.amount_minor),
        ],
    )

    await _transition(session, payment, PaymentStatus.CAPTURED)
    _emit(session, payment, "payment.captured")
    return payment


async def void(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    payment = await _locked(session, payment_id)

    # Same reasoning as capture(): the state machine must be checked before
    # any ledger write, or a repeated void hits uq_ledger_tx_idempotency and
    # surfaces as a 500 instead of a 409.
    assert_can_transition(payment.status, PaymentStatus.VOIDED)

    clearing = await _clearing_account(session, payment.currency)

    await ledger.post(
        session,
        kind="payment.void",
        idempotency_key=f"void:{payment.id}",
        postings=[
            Posting(clearing.id, -payment.amount_minor),
            Posting(payment.payer_account_id, +payment.amount_minor),
        ],
    )

    await _transition(session, payment, PaymentStatus.VOIDED)
    _emit(session, payment, "payment.voided")
    return payment


async def _locked(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    """
    Read the payment row with FOR UPDATE so two concurrent captures serialize.

    Belt and braces alongside the ledger's own unique idempotency key: even if
    both requests got past this, the second ledger.post() would violate
    uq_ledger_tx_idempotency and abort. Two independent defences, because
    double-spending is the failure you cannot apologise your way out of.
    """
    payment = (
        await session.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if payment is None:
        raise PaymentError(f"payment {payment_id} not found")
    return payment
