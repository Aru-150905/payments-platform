"""
Order placement: lock the resting book, run the PURE matcher over it, settle
every resulting trade into the ledger, persist the outcome, emit events.

This is the one place database access and the matching decision meet. See
docs/adr/0008-matching-engine.md for why that split exists and what each
side of it is and isn't responsible for. Read app/domain/matching.py first
— this module's whole job is translating between that pure world (frozen
dataclasses, no I/O) and this one (ORM rows, row locks, a Kafka outbox).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Account,
    AccountType,
    Balance,
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    OutboxEvent,
    Trade,
)
from app.domain.matching import BookOrder, Fill, OrderBook, match
from app.domain.order_state_machine import assert_can_transition
from app.events.topics import ORDER_EVENTS
from app.services import ledger
from app.services.ledger import Posting
from app.services.payments import CLEARING_OWNER


class TradingError(Exception):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

async def _position_account(
    session: AsyncSession, owner_id: str, instrument: Instrument
) -> Account:
    """
    Find-or-create the (owner, instrument) position account — an ordinary
    `accounts` row, denominated in the instrument's symbol instead of a real
    currency (ADR 0008 Decision 4).

    `allow_negative=False` for every owner except CLEARING_OWNER ("platform",
    the same sentinel app/services/payments.py's clearing account already
    uses). That's not a payments detail leaking in — it's the answer to a
    real question the no-shorting rule raises: if NO position account can
    ever go negative, where does an instrument's very first share come
    from? Nobody can sell what they don't already hold, so without one
    account allowed to go negative, a brand-new instrument could never
    trade at all. "platform" plays the same role here it already plays for
    cash: the account real shares enter and leave the system through,
    allowed to run negative because it represents supply, not a trader's
    holdings. A real venue would call this a treasury or issuer account;
    this project reuses the one privileged identity it already has rather
    than inventing a second concept to express the same idea.

    The find-then-create here has the same race window M2's idempotency
    keys exist to close: two concurrent FIRST trades in this instrument by
    this owner could both see "no position account yet." That's what
    uq_accounts_owner_name_currency (migration 0003) closes at the database
    level. UNLIKE payments.py's create_and_authorize(), which explicitly
    catches that exact shape of race and re-reads the winner, this function
    does NOT catch the resulting IntegrityError — it's narrow enough (same
    owner, same brand-new instrument, same instant) that doubling the
    exception-handling logic for it wasn't judged worth the complexity at
    this scope. A caller who hits it sees a raw 500, not a clean retry.
    Known, accepted, not silently ignored.
    """
    existing = (
        await session.execute(
            select(Account).where(
                Account.owner_id == owner_id,
                Account.name == f"position:{instrument.symbol}",
                Account.currency == instrument.symbol,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    account = Account(
        owner_id=owner_id,
        name=f"position:{instrument.symbol}",
        account_type=AccountType.ASSET,
        currency=instrument.symbol,
        allow_negative=(owner_id == CLEARING_OWNER),
    )
    session.add(account)
    # Flush now, before touching anything else — same reasoning as
    # ledger.post()'s idempotency-key flush: a conflict surfaces here, not
    # after other writes have piled up in the same transaction.
    await session.flush()
    session.add(Balance(account_id=account.id, currency=instrument.symbol, balance_minor=0))
    await session.flush()
    return account


def _to_book_order(order: Order) -> BookOrder:
    """ORM row -> the matcher's pure input type. See app/domain/matching.py."""
    return BookOrder(
        order_id=order.id,
        side=order.side,
        order_type=order.order_type,
        limit_price_minor=order.limit_price_minor,
        remaining_quantity=order.quantity - order.filled_quantity,
        sequence=order.sequence,
    )


def _emit(session: AsyncSession, *, instrument: Instrument, event_type: str, payload: dict) -> None:
    session.add(
        OutboxEvent(
            topic=ORDER_EVENTS,
            event_type=event_type,
            # Keyed by the INSTRUMENT, not by the order's or trade's own id
            # — see app/events/topics.py's ORDER_EVENTS comment. Every
            # order/trade event for one instrument has to land on the same
            # Kafka partition to stay ordered relative to each other, and a
            # partition is chosen by key.
            aggregate_id=str(instrument.id),
            payload=payload,
        )
    )


async def _settle_fill(
    session: AsyncSession,
    *,
    instrument: Instrument,
    fill: Fill,
    buyer: tuple[str, uuid.UUID, uuid.UUID],
    seller: tuple[str, uuid.UUID, uuid.UUID],
) -> None:
    """
    One fill -> one ledger transaction (cash leg + position leg) + one Trade
    row. `buyer`/`seller` are (owner_id, cash_account_id, position_account_id)
    triples — the caller resolves WHICH party is buyer/seller and what
    accounts they use; this function only knows how to settle a fill once
    that's decided.
    """
    _, buyer_cash, buyer_position = buyer
    _, seller_cash, seller_position = seller
    cash_amount = fill.price_minor * fill.quantity
    trade_id = uuid.uuid4()

    postings = [
        Posting(buyer_cash, -cash_amount),      # cash leaves the buyer
        Posting(seller_cash, +cash_amount),     # cash arrives at the seller
        Posting(buyer_position, +fill.quantity),  # shares arrive at the buyer
        Posting(seller_position, -fill.quantity),  # shares leave the seller
    ]

    try:
        ledger_tx_id: uuid.UUID | None = await ledger.post(
            session,
            kind="trade.settle",
            idempotency_key=f"trade:{trade_id}",
            postings=postings,
        )
    except ledger.UnbalancedTransaction:
        # These 4 postings are constructed from a single price and quantity
        # above, so the ONLY way validate_currency_balance() can reject them
        # is a self-match collapsing to fewer than two distinct accounts
        # (same owner, same cash account, same position account on both
        # sides) — see ADR 0008 Decision 4. Not an error: the trade genuinely
        # happened, it just moved zero real money.
        ledger_tx_id = None

    session.add(
        Trade(
            id=trade_id,
            instrument_id=instrument.id,
            buy_order_id=fill.buy_order_id,
            sell_order_id=fill.sell_order_id,
            price_minor=fill.price_minor,
            quantity=fill.quantity,
            ledger_transaction_id=ledger_tx_id,
        )
    )
    _emit(
        session,
        instrument=instrument,
        event_type="trade.executed",
        payload={
            "trade_id": str(trade_id),
            "instrument_id": str(instrument.id),
            "symbol": instrument.symbol,
            "buy_order_id": str(fill.buy_order_id),
            "sell_order_id": str(fill.sell_order_id),
            "price_minor": fill.price_minor,
            "quantity": fill.quantity,
            "ledger_transaction_id": str(ledger_tx_id) if ledger_tx_id else None,
        },
    )


# --------------------------------------------------------------------------
# the one public entry point
# --------------------------------------------------------------------------

async def place_order(
    session: AsyncSession,
    *,
    idempotency_key: str,
    instrument_id: uuid.UUID,
    cash_account_id: uuid.UUID,
    side: OrderSide,
    order_type: OrderType,
    quantity: int,
    limit_price_minor: int | None,
) -> Order:
    """
    Place an order: match it against the resting book, settle whatever it
    fills, persist everything, and return the order in its final state for
    this call (which may already be terminal — see rule 5 in
    app/domain/matching.py's docstring on why a market order never rests).

    Everything below runs inside the CALLER's transaction — like
    ledger.post() and payments.create_and_authorize(), this function never
    commits. If ANY leg of ANY trade fails to settle (ledger.InsufficientFunds,
    most likely — see ADR 0008 Decision 4's "gap this accepts" paragraph),
    the exception propagates and the caller's `session.begin()` rolls back
    the WHOLE thing: no order row, no trades, no change to any resting
    order's state. The book is exactly as it was before this call started.

    No separate `owner_id` parameter, unlike most of this codebase's other
    write paths — it's read directly off `cash_account_id` instead of being
    supplied alongside it and cross-checked. There's nothing to reconcile:
    the account IS the caller's identity for this order, the same way
    `payer_account_id` alone (no separate payer owner) is enough for a
    payment.
    """
    # --- 0. idempotency: has this exact request already been placed? ------
    existing = (
        await session.execute(select(Order).where(Order.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # --- 1. resolve and validate the instrument and cash account ----------
    instrument = await session.get(Instrument, instrument_id)
    if instrument is None:
        raise TradingError(f"instrument {instrument_id} not found")

    cash_account = await session.get(Account, cash_account_id)
    if cash_account is None:
        raise TradingError(f"cash account {cash_account_id} not found")
    owner_id = cash_account.owner_id
    if cash_account.currency != instrument.quote_currency:
        raise TradingError(
            f"cash account is in {cash_account.currency}, "
            f"instrument settles in {instrument.quote_currency}"
        )

    position_account = await _position_account(session, owner_id, instrument)

    # --- 2. lock the resting book for this instrument -----------------------
    # Coarse on purpose: every OPEN/PARTIALLY_FILLED order for the
    # instrument, not just the handful an incoming order will actually
    # touch. This is the DB-row-lock stand-in ADR 0008 Decision 3 contrasts
    # with a real exchange's single-threaded in-memory matching engine.
    resting_rows = (
        await session.execute(
            select(Order)
            .where(
                Order.instrument_id == instrument_id,
                Order.status.in_((OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)),
            )
            .with_for_update()
        )
    ).scalars().all()
    resting_by_id = {o.id: o for o in resting_rows}

    book = OrderBook(
        bids=tuple(_to_book_order(o) for o in resting_rows if o.side is OrderSide.BUY),
        asks=tuple(_to_book_order(o) for o in resting_rows if o.side is OrderSide.SELL),
    )

    # --- 3. the PURE decision ------------------------------------------------
    order_id = uuid.uuid4()
    incoming = BookOrder(
        order_id=order_id,
        side=side,
        order_type=order_type,
        limit_price_minor=limit_price_minor,
        remaining_quantity=quantity,
        # 0 is a placeholder, not a real time-priority position: this value
        # is only ever read back out via result.resting, which this
        # function discards in favour of the REAL sequence Identity()
        # assigns to the Order row below once it's flushed. It plays no
        # role in matching THIS call — an incoming order is by definition
        # later than everything already resting.
        sequence=0,
    )
    result = match(book, incoming)

    # --- 4. settle every fill, update every resting order it touched --------
    for fill in result.fills:
        resting_id = fill.sell_order_id if side is OrderSide.BUY else fill.buy_order_id
        resting_order = resting_by_id[resting_id]
        resting_party = (
            resting_order.owner_id, resting_order.cash_account_id, resting_order.position_account_id
        )
        incoming_party = (owner_id, cash_account_id, position_account.id)
        buyer, seller = (
            (incoming_party, resting_party) if side is OrderSide.BUY
            else (resting_party, incoming_party)
        )

        await _settle_fill(session, instrument=instrument, fill=fill, buyer=buyer, seller=seller)

        new_filled = resting_order.filled_quantity + fill.quantity
        new_status = (
            OrderStatus.FILLED if new_filled == resting_order.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        assert_can_transition(resting_order.status, new_status)
        resting_order.filled_quantity = new_filled
        resting_order.status = new_status

    # --- 5. the incoming order's own final state -----------------------------
    filled_quantity = sum(f.quantity for f in result.fills)
    if order_type is OrderType.MARKET:
        # A market order is never OPEN and never PARTIALLY_FILLED (rule 5,
        # app/domain/matching.py) — whatever it didn't fill is cancelled,
        # not left resting.
        status = OrderStatus.FILLED if filled_quantity == quantity else OrderStatus.CANCELLED
    elif filled_quantity == quantity:
        status = OrderStatus.FILLED
    elif filled_quantity == 0:
        status = OrderStatus.OPEN
    else:
        status = OrderStatus.PARTIALLY_FILLED

    order = Order(
        id=order_id,
        idempotency_key=idempotency_key,
        owner_id=owner_id,
        instrument_id=instrument_id,
        cash_account_id=cash_account_id,
        position_account_id=position_account.id,
        side=side,
        order_type=order_type,
        limit_price_minor=limit_price_minor,
        quantity=quantity,
        filled_quantity=filled_quantity,
        status=status,
    )
    session.add(order)
    await session.flush()  # assigns order.sequence via Identity()

    _emit(
        session,
        instrument=instrument,
        event_type="order.placed",
        payload={
            "order_id": str(order.id),
            "instrument_id": str(instrument.id),
            "symbol": instrument.symbol,
            "owner_id": order.owner_id,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "limit_price_minor": order.limit_price_minor,
            "quantity": order.quantity,
            "filled_quantity": order.filled_quantity,
            "status": order.status.value,
        },
    )

    return order
