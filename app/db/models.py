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
    Identity,
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


class OrderSide(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(enum.StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(enum.StrEnum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"

    # Covers two different real causes under one terminal status, on purpose
    # — "will never trade any more, and was not fully filled": an explicit
    # user cancellation (no route exists for this yet — see
    # app/domain/order_state_machine.py) AND a market order's unfilled
    # remainder, which cannot rest in the book by definition and so is
    # immediately terminal the moment it's placed. Distinguishing "the user
    # cancelled it" from "the market couldn't fill it" would need a second
    # status with no behavioural difference from this one; not worth it at
    # this scope. See ADR 0008.
    CANCELLED = "cancelled"


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

    # Widened from VARCHAR(3) and relaxed from a strict ISO-4217 check in
    # M6: `currency` is now "unit of account", not always a real currency —
    # a position account's currency is the instrument's symbol (e.g.
    # "AAPL"), which is longer than 3 characters and isn't an ISO code at
    # all. See ADR 0008 Decision 4 for why reusing this column, instead of
    # adding a parallel instrument concept, is the whole point.
    currency: Mapped[str] = mapped_column(String(16))

    # Customer wallets must not go negative; the platform's own clearing and
    # revenue accounts must be allowed to, or nothing can ever settle. Reused
    # unchanged for position accounts: False here is what "no shorting"
    # means for a trading position (ADR 0008) — selling shares you don't
    # hold hits the exact same InsufficientFunds a payments overdraft does.
    allow_negative: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # Either a real ISO-4217 code (3 letters) or an instrument symbol
        # (1-16 uppercase letters/digits) — the two things `currency` is now
        # allowed to mean. Not trying to tell them apart by shape beyond
        # that; see ADR 0008's consequences on the narrow collision risk.
        CheckConstraint("currency ~ '^[A-Z0-9]{1,16}$'", name="ck_accounts_currency_iso"),
        # Position accounts are auto-provisioned, one per (owner, currency)
        # — see app/services/trading.py's _position_account(). Without this,
        # two concurrent first trades in the same instrument by the same
        # owner could both see "no position account yet" and both insert
        # one, the same race M2's idempotency keys exist to close elsewhere.
        # `name` is included (not just owner+currency) so it does NOT
        # constrain ordinary payments accounts, which may legitimately have
        # more than one wallet in the same currency.
        UniqueConstraint(
            "owner_id", "name", "currency", name="uq_accounts_owner_name_currency"
        ),
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
    currency: Mapped[str] = mapped_column(String(16))  # see Account.currency
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
    currency: Mapped[str] = mapped_column(String(16))  # see Account.currency

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
# Trading (M6) — see docs/adr/0008-matching-engine.md
# --------------------------------------------------------------------------

class Instrument(Base):
    """
    A tradeable thing. Deliberately thin — no lot size, no tick size, no
    trading-hours calendar. `quote_currency` is the one fact every order and
    trade settlement genuinely needs: which real currency the cash leg moves
    in. Everything else a real venue tracks per instrument is out of scope.
    """
    __tablename__ = "instruments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    symbol: Mapped[str] = mapped_column(String(16), unique=True)
    quote_currency: Mapped[str] = mapped_column(String(3))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("symbol ~ '^[A-Z0-9]{1,16}$'", name="ck_instruments_symbol_format"),
        CheckConstraint(
            "quote_currency ~ '^[A-Z]{3}$'", name="ck_instruments_quote_currency_iso"
        ),
    )


class Order(Base):
    """
    One order, resting or not. `owner_id` and `cash_account_id` are stored
    directly rather than requiring a join — same reasoning as Payment's
    payer/payee columns: query and index convenience, and every row stays
    self-describing on its own.

    No `version` column, unlike Payment. Payment uses optimistic concurrency
    (compare-and-swap on `version`) because two captures race on ONE row.
    An order's mutation instead happens under app/services/trading.py's
    coarse `SELECT ... FOR UPDATE` over every resting order for the
    instrument — a pessimistic lock on the whole book, taken BEFORE matching
    starts. That lock already serializes every writer touching this row, so
    a second concurrency mechanism on top of it would be redundant, not
    extra-safe.
    """
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))

    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    cash_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id")
    )
    position_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id")
    )

    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide, native_enum=False, length=8))
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, native_enum=False, length=8)
    )
    # NULL for a market order — it has no limit, by definition. The CHECK
    # constraint below is what actually enforces that pairing; this column
    # being nullable is necessary but not sufficient on its own.
    limit_price_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    quantity: Mapped[int] = mapped_column(BigInteger)
    filled_quantity: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=20), default=OrderStatus.OPEN
    )

    # Time priority's tie-breaker. NOT created_at: two orders can carry the
    # same timestamp (same millisecond, or worse — a clock that steps
    # backward under NTP correction), and time priority needs a TOTAL order
    # with no ties, ever. A database-generated, gap-tolerant, strictly
    # increasing sequence is the standard way a real exchange solves this
    # same problem, and Identity() is Postgres doing exactly that: it hands
    # out the next integer atomically, independent of wall-clock time.
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency"),
        CheckConstraint("quantity > 0", name="ck_orders_quantity_positive"),
        CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity",
            name="ck_orders_filled_quantity_in_range",
        ),
        # UPPERCASE, not order_type.value ('limit'/'market'): SQLAlchemy's
        # Enum(native_enum=False) stores an enum MEMBER'S NAME by default,
        # not its .value — the exact same reason `accounts.account_type`
        # holds "LIABILITY" and `payments.status` holds "AUTHORIZED", not
        # their lowercase .value counterparts. A CHECK constraint is raw SQL
        # comparing against the literal stored string, so it has to match
        # what's actually written to the column, not the enum's Python-side
        # value — caught live (see M6's commit history) when this
        # constraint rejected a perfectly legal insert.
        CheckConstraint(
            "(order_type = 'LIMIT' AND limit_price_minor IS NOT NULL AND limit_price_minor > 0) "
            "OR (order_type = 'MARKET' AND limit_price_minor IS NULL)",
            name="ck_orders_limit_price_matches_type",
        ),
    )


class Trade(Base):
    """
    One match. `ledger_transaction_id` is nullable for exactly one reason:
    a self-match that settles to zero net movement on every account it
    touches has nothing for the ledger to record — see ADR 0008 Decision 4.
    A NULL here means "this trade genuinely happened, at this price and
    quantity, and moved zero real money," not "settlement is pending" or
    "something went wrong."
    """
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), index=True
    )
    buy_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id")
    )
    sell_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id")
    )

    # The maker (the resting order) sets the price; the taker (the incoming
    # order) accepts it — standard price-time-priority convention. See
    # app/domain/matching.py.
    price_minor: Mapped[int] = mapped_column(BigInteger)
    quantity: Mapped[int] = mapped_column(BigInteger)

    ledger_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("price_minor > 0", name="ck_trades_price_positive"),
        CheckConstraint("quantity > 0", name="ck_trades_quantity_positive"),
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
