"""
Price-time-priority matching, as a pure function over an in-memory snapshot.

See docs/adr/0008-matching-engine.md Decision 1 for why this file imports
nothing from SQLAlchemy, aiokafka, or anywhere else in this codebase that
touches a database or a network. Every type below is a plain, frozen
dataclass. match() takes an OrderBook and one incoming order and returns
what happened — it never reaches outside itself to find out anything.

The rules, stated once so the code below only has to implement them:

  1. PRICE priority: a buy matches the CHEAPEST resting sell first; a sell
     matches the HIGHEST resting buy first.
  2. TIME priority: among resting orders at the same price, the one with
     the lower `sequence` (placed earlier) matches first.
  3. The RESTING order sets the execution price, not the incoming order —
     "the maker sets the price, the taker accepts it." A resting buy at
     100 crossed by an incoming sell at 95 executes at 100, not 95 and not
     the midpoint 97.5 — the seller is happy to get more than they asked,
     and the buyer pays exactly what they already committed to.
  4. A MARKET order has no price limit — it crosses at whatever price is
     available, sweeping multiple price levels if one level's quantity
     isn't enough, until it's fully filled or the opposite side is empty.
  5. A MARKET order never rests. Whatever quantity it can't fill
     immediately is simply not placed into the book — there is no price to
     rest it at, by definition.
  6. A LIMIT order that isn't fully filled rests in the book at its own
     price and (freshly assigned, by the CALLER before this function is
     invoked) sequence number.

Ownership is NOT checked anywhere in this file — a buy and a sell from the
same owner_id are matched exactly like any other pair. See ADR 0008 Decision
3 on why self-trade prevention is deliberately out of scope for the matcher
itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from app.db.models import OrderSide, OrderType


@dataclass(frozen=True)
class BookOrder:
    """
    One order, as the matcher sees it — NOT the ORM Order model. Keeping
    this a separate, minimal type (rather than importing app.db.models.Order)
    is what makes it possible to construct a test fixture in one line with
    no database, no session, and no owner/account plumbing the matcher
    itself never looks at.
    """

    order_id: uuid.UUID
    side: OrderSide
    order_type: OrderType
    # None for a market order. A resting order is NEVER a market order (see
    # match()'s precondition check) — a market order that didn't fully fill
    # is discarded, per rule 5 above, so this field being None only ever
    # shows up on the INCOMING side of a call.
    limit_price_minor: int | None
    remaining_quantity: int
    # Time priority's tie-breaker — lower sequence matches first at an equal
    # price. See app/db/models.py Order.sequence for why this exists as a
    # dedicated field instead of a timestamp.
    sequence: int


@dataclass(frozen=True)
class OrderBook:
    """Resting orders for ONE instrument. Order within each tuple does not
    matter — match() sorts before doing anything else (see its docstring)."""

    bids: tuple[BookOrder, ...] = ()  # resting BUY orders
    asks: tuple[BookOrder, ...] = ()  # resting SELL orders


@dataclass(frozen=True)
class Fill:
    """One match between two orders, at one price, for one quantity."""

    buy_order_id: uuid.UUID
    sell_order_id: uuid.UUID
    price_minor: int
    quantity: int


@dataclass(frozen=True)
class MatchResult:
    fills: tuple[Fill, ...]
    book: OrderBook  # the book AFTER this order was processed
    # The portion of the incoming order that rests in `book` afterward, or
    # None if it fully filled, or None if it was a market order (rule 5) —
    # this is exactly book.bids/asks' new entry for the incoming order, if
    # any, handed back separately so a caller doesn't have to diff the old
    # and new book to find out what happened to the order it just placed.
    resting: BookOrder | None


def _crosses(incoming: BookOrder, resting: BookOrder) -> bool:
    """Would `incoming` accept trading against `resting` at resting's price?"""
    if incoming.order_type is OrderType.MARKET:
        return True  # no price limit — always willing
    if incoming.side is OrderSide.BUY:
        # Willing to buy at resting's ask if not paying more than my limit.
        return incoming.limit_price_minor >= resting.limit_price_minor
    # Willing to sell into resting's bid if not accepting less than my limit.
    return incoming.limit_price_minor <= resting.limit_price_minor


def _priority_key(side: OrderSide):
    """
    The sort key that turns "price-time priority" into a plain list sort.
    Applied to the OPPOSITE side from `side` — i.e. call
    _priority_key(OrderSide.BUY) to sort the ASKS an incoming buy matches
    against (cheapest first), and _priority_key(OrderSide.SELL) to sort the
    BIDS an incoming sell matches against (highest first).
    """
    if side is OrderSide.BUY:
        return lambda o: (o.limit_price_minor, o.sequence)  # asks: cheapest, then earliest
    return lambda o: (-o.limit_price_minor, o.sequence)  # bids: highest, then earliest


def _insert_resting(same_side: tuple[BookOrder, ...], order: BookOrder) -> tuple[BookOrder, ...]:
    """
    Insert `order` into its own side of the book at the correct
    price-time-priority position. A linear scan, not a bisect — order books
    in this project are small (portfolio-project scale, not an exchange's
    real depth), and a scan is far easier to read than a bisect call with a
    hand-rolled comparator, for a cost that doesn't matter here.
    """
    # _priority_key(side) sorts the side OPPOSITE `side` — so to sort a
    # resting order's OWN side the same way a future opposite-side incoming
    # order will scan it, ask for the key belonging to whichever side this
    # order is NOT.
    key = _priority_key(OrderSide.SELL if order.side is OrderSide.BUY else OrderSide.BUY)
    result = list(same_side)
    i = 0
    while i < len(result) and key(result[i]) <= key(order):
        i += 1
    result.insert(i, order)
    return tuple(result)


def match(book: OrderBook, incoming: BookOrder) -> MatchResult:
    """
    Apply one incoming order to a book snapshot. Pure: same inputs, same
    outputs, every time, no matter when or how many times it's called.

    `incoming.remaining_quantity` is read as "how much is left to fill" —
    for a brand-new order this is just its full quantity, but nothing here
    assumes that; a caller could in principle hand in an order that's
    already partially filled elsewhere. `incoming.sequence` matters only if
    this order ends up resting (rule 6); it plays no role in matching THIS
    call, since an incoming order is, by definition, later than everything
    already in the book.
    """
    if any(o.order_type is OrderType.MARKET for o in book.bids + book.asks):
        # A market order that didn't fully fill is discarded, never
        # inserted (rule 5) — so a market order should never be able to
        # reach the book in the first place. Catching this here turns a
        # caller's bug into a clear message instead of a None comparison
        # crash deep inside a sort key.
        raise ValueError("a market order can never rest in the book")

    opposite = list(book.asks if incoming.side is OrderSide.BUY else book.bids)
    opposite.sort(key=_priority_key(incoming.side))

    fills: list[Fill] = []
    remaining_qty = incoming.remaining_quantity

    while remaining_qty > 0 and opposite:
        best = opposite[0]
        if not _crosses(incoming, best):
            # Sorted best-first: if the best available doesn't cross,
            # nothing behind it (worse price) can either.
            break

        fill_qty = min(remaining_qty, best.remaining_quantity)
        # Rule 3: the RESTING order's price, always — best.limit_price_minor
        # is never None here, because the precondition check above already
        # ruled out a market order resting in the book.
        execution_price = best.limit_price_minor

        if incoming.side is OrderSide.BUY:
            fills.append(Fill(incoming.order_id, best.order_id, execution_price, fill_qty))
        else:
            fills.append(Fill(best.order_id, incoming.order_id, execution_price, fill_qty))

        remaining_qty -= fill_qty
        best_remaining = best.remaining_quantity - fill_qty
        if best_remaining == 0:
            opposite.pop(0)
        else:
            opposite[0] = replace(best, remaining_quantity=best_remaining)

    resting: BookOrder | None = None
    if incoming.order_type is OrderType.LIMIT and remaining_qty > 0:
        resting = replace(incoming, remaining_quantity=remaining_qty)
    # A market order's leftover (remaining_qty > 0 with nothing left to
    # cross) is silently dropped here — rule 5. Nothing to raise: an
    # unfilled market-order remainder is an ordinary, expected outcome, not
    # an error.

    same_side = book.bids if incoming.side is OrderSide.BUY else book.asks
    new_same_side = _insert_resting(same_side, resting) if resting is not None else same_side

    if incoming.side is OrderSide.BUY:
        new_book = OrderBook(bids=new_same_side, asks=tuple(opposite))
    else:
        new_book = OrderBook(bids=tuple(opposite), asks=new_same_side)

    return MatchResult(fills=tuple(fills), book=new_book, resting=resting)
