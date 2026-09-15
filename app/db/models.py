"""
Schema.

Money rule for the whole project: amounts are BIGINT in the currency's minor
unit (paise, cents). Never float, never NUMERIC-with-rounding-in-Python.
0.1 + 0.2 != 0.3 in binary floating point, and a ledger that is off by one
paise is a broken ledger.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class AccountType(enum.StrEnum):
    """
    Accounting classification, not a business label.

    ASSET / EXPENSE increase on debit. LIABILITY / EQUITY / REVENUE increase on
    credit. This matters because "the user's balance" is a LIABILITY to you —
    you owe them that money. Getting this backwards is why homegrown ledgers
    end up with negative totals nobody can explain.
    """
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class PaymentStatus(enum.StrEnum):
    INITIATED = "initiated"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    VOIDED = "voided"
    FAILED = "failed"
    REFUNDED = "refunded"


# --------------------------------------------------------------------------
# Accounts and balances
# --------------------------------------------------------------------------

class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, native_enum=False, length=16)
    )
    currency: Mapped[str] = mapped_column(String(3))

    # Customer wallets must not go negative; the platform's own clearing and
    # revenue accounts must be allowed to, or nothing can ever settle.
    allow_negative: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_accounts_currency_iso"),
    )


class Balance(Base):
    """
    A cached, running balance.

    Strictly speaking this is redundant — the truth is SUM(ledger_entries).
    It exists because that SUM gets slower every single day the system runs,
    and reading a balance is the hottest query in a payments system.

    The redundancy is safe only because it is written in the SAME database
    transaction as the entries that change it, and because the reconciliation
    job (M4) re-derives it from scratch and alarms on any mismatch.
    """
    __tablename__ = "balances"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), primary_key=True
    )
    balance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

class LedgerTransaction(Base):
    """
    A group of entries that must sum to zero. This is the unit of atomicity.
    """
    __tablename__ = "ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    kind: Mapped[str] = mapped_column(String(48))

    # The idempotency key is UNIQUE at the database level, not checked in
    # Python. A "check then insert" in application code has a race window:
    # two concurrent retries both read "not found" and both insert. Only the
    # database can settle that.
    idempotency_key: Mapped[str] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_tx_idempotency"),
    )


class LedgerEntry(Base):
    """
    One side of a movement. Append-only: never UPDATE, never DELETE.
    A mistake is corrected by posting a reversing transaction, so the history
    of what you believed and when stays intact and auditable.
    """
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id"), index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id")
    )

    # Signed: positive = debit, negative = credit. One signed column instead of
    # (direction, amount) so "sum to zero" is a plain SUM() the database can
    # verify, rather than a CASE expression everyone forgets to write.
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="ck_entry_nonzero"),
        Index("ix_entries_account_created", "account_id", "created_at"),
    )


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------

class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))

    payer_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id")
    )
    payee_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id")
    )

    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False, length=16), default=PaymentStatus.INITIATED
    )

    # Optimistic concurrency. Two concurrent captures both read version=1;
    # the UPDATE ... WHERE version=1 succeeds once and affects 0 rows the
    # second time, which we turn into a 409 instead of a double capture.
    version: Mapped[int] = mapped_column(BigInteger, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency"),
        CheckConstraint("amount_minor > 0", name="ck_payment_positive"),
        CheckConstraint(
            "payer_account_id <> payee_account_id",
            name="ck_payment_distinct_accounts",
        ),
    )


# --------------------------------------------------------------------------
# Read model
# --------------------------------------------------------------------------

class ReadModelPayment(Base):
    """
    Projected purely from payments.payment.v1 by app/events/consumer.py — see
    ADR 0006. No other code path writes this table. Every column here is a
    direct copy of the event payload as of the last event applied; there is
    no running total or counter, which is what makes replaying the same
    event twice (a redelivery, or a full rebuild) safe rather than additive.
    """
    __tablename__ = "read_model_payments"

    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    payer_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    payee_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))

    # The event, not the row, that produced this state. Useful for debugging
    # a stuck or out-of-order projection; not consulted by the projection
    # logic itself, which trusts Kafka's per-partition ordering instead.
    last_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    last_event_type: Mapped[str] = mapped_column(String(64))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --------------------------------------------------------------------------
# Outbox and consumer dedup
# --------------------------------------------------------------------------

class OutboxEvent(Base):
    """
    The transactional outbox.

    Problem it solves: you cannot commit to Postgres and publish to Kafka
    atomically — they are two systems with no shared transaction. Publishing
    directly means a crash between commit and publish silently loses the event,
    and publishing first means a rolled-back transaction emits an event for
    something that never happened.

    Fix: write the event as a ROW in the same transaction as the business
    change. Either both land or neither does. A separate relay process then
    reads unpublished rows and pushes them to Kafka. That relay can crash and
    retry freely, because at-least-once publishing is already handled by the
    consumer's dedup table below.
    """
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=_uuid, unique=True
    )
    topic: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Partial index: only unpublished rows are indexed. The table grows
        # forever but the index the relay scans stays tiny.
        Index(
            "ix_outbox_unpublished",
            "id",
            postgresql_where=(published_at.is_(None)),
        ),
    )


class ProcessedEvent(Base):
    """
    Consumer-side dedup. Kafka gives at-least-once delivery, so a redelivery
    after a crash is normal, not exceptional. The consumer inserts the event_id
    here inside the same transaction as its side effects; a duplicate hits the
    primary key and is skipped.

    Keyed by (event_id, consumer_group) because two different groups must each
    be allowed to process the same event once.
    """
    __tablename__ = "processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    consumer_group: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
