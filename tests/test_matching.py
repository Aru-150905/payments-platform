"""
Thorough, infra-free tests for app/domain/matching.py's match().

This is the one piece of M6 the roadmap singled out as not-to-shortcut, and
the reason is exactly what these tests are for: price-time priority across
two sides of a book, partial fills, and a market order sweeping several
levels are the kind of logic that looks right on a read-through and is wrong
in a case nobody traced by hand. Every test below constructs its own book by
hand — no fixtures shared across tests, no accumulated state — so a failure
always points at ONE specific rule, not at some other test's leftovers.
"""

import uuid

import pytest

from app.db.models import OrderSide, OrderType
from app.domain.matching import BookOrder, OrderBook, match

BUY, SELL = OrderSide.BUY, OrderSide.SELL
LIMIT, MARKET = OrderType.LIMIT, OrderType.MARKET


def order_id() -> uuid.UUID:
    return uuid.uuid4()


def limit_order(side, price, qty, sequence, order_id_=None) -> BookOrder:
    return BookOrder(
        order_id=order_id_ or order_id(),
        side=side,
        order_type=LIMIT,
        limit_price_minor=price,
        remaining_quantity=qty,
        sequence=sequence,
    )


def market_order(side, qty, sequence=999) -> BookOrder:
    return BookOrder(
        order_id=order_id(),
        side=side,
        order_type=MARKET,
        limit_price_minor=None,
        remaining_quantity=qty,
        sequence=sequence,
    )


# --------------------------------------------------------------------------
# empty book
# --------------------------------------------------------------------------

def test_limit_order_against_empty_book_rests_with_no_fills():
    incoming = limit_order(BUY, 100, 10, sequence=1)
    result = match(OrderBook(), incoming)

    assert result.fills == ()
    assert result.resting == incoming
    assert result.book.bids == (incoming,)
    assert result.book.asks == ()


def test_market_order_against_empty_book_produces_nothing_and_does_not_rest():
    incoming = market_order(BUY, 10)
    result = match(OrderBook(), incoming)

    assert result.fills == ()
    assert result.resting is None
    assert result.book == OrderBook()  # unchanged — nothing to add, nothing to remove


# --------------------------------------------------------------------------
# price priority: the CHEAPEST ask fills a buy first, the HIGHEST bid fills
# a sell first — regardless of the order the resting orders are passed in.
# --------------------------------------------------------------------------

def test_buy_matches_the_cheapest_ask_first():
    cheap = limit_order(SELL, 100, 10, sequence=1)
    expensive = limit_order(SELL, 105, 10, sequence=2)
    # Deliberately passed in the "wrong" order — match() must sort itself.
    book = OrderBook(asks=(expensive, cheap))

    incoming = limit_order(BUY, 105, 10, sequence=3)
    result = match(book, incoming)

    assert len(result.fills) == 1
    assert result.fills[0].sell_order_id == cheap.order_id
    assert result.fills[0].price_minor == 100  # the maker's (cheap ask's) price
    # The more expensive ask is untouched, still resting.
    assert result.book.asks == (expensive,)


def test_sell_matches_the_highest_bid_first():
    low = limit_order(BUY, 95, 10, sequence=1)
    high = limit_order(BUY, 99, 10, sequence=2)
    book = OrderBook(bids=(low, high))

    incoming = limit_order(SELL, 95, 10, sequence=3)
    result = match(book, incoming)

    assert len(result.fills) == 1
    assert result.fills[0].buy_order_id == high.order_id
    assert result.fills[0].price_minor == 99
    assert result.book.bids == (low,)


def test_a_limit_order_that_does_not_cross_the_best_price_rests_untouched():
    """A buy at 90 must not cross a cheapest ask of 100 — no fill at all."""
    resting_ask = limit_order(SELL, 100, 10, sequence=1)
    incoming = limit_order(BUY, 90, 10, sequence=2)

    result = match(OrderBook(asks=(resting_ask,)), incoming)

    assert result.fills == ()
    assert result.book.asks == (resting_ask,)
    assert result.resting == incoming


# --------------------------------------------------------------------------
# time priority at equal price
# --------------------------------------------------------------------------

def test_time_priority_at_equal_price_earlier_sequence_fills_first():
    first = limit_order(SELL, 100, 10, sequence=1)
    second = limit_order(SELL, 100, 10, sequence=2)
    # Passed in reverse arrival order — match() must not use list order.
    book = OrderBook(asks=(second, first))

    incoming = limit_order(BUY, 100, 10, sequence=3)
    result = match(book, incoming)

    assert len(result.fills) == 1
    assert result.fills[0].sell_order_id == first.order_id
    assert result.book.asks == (second,)


def test_time_priority_holds_even_with_three_orders_at_the_same_price():
    o1 = limit_order(SELL, 50, 5, sequence=10)
    o2 = limit_order(SELL, 50, 5, sequence=5)   # earliest
    o3 = limit_order(SELL, 50, 5, sequence=20)
    book = OrderBook(asks=(o1, o2, o3))

    incoming = limit_order(BUY, 50, 5, sequence=30)
    result = match(book, incoming)

    assert result.fills[0].sell_order_id == o2.order_id
    # o1 and o3 remain, still ordered by sequence for the NEXT incoming order.
    assert [o.order_id for o in result.book.asks] == [o1.order_id, o3.order_id]


# --------------------------------------------------------------------------
# partial fills
# --------------------------------------------------------------------------

def test_incoming_order_partially_fills_and_rests_with_the_remainder():
    resting = limit_order(SELL, 100, 5, sequence=1)
    incoming = limit_order(BUY, 100, 10, sequence=2)  # wants more than is offered

    result = match(OrderBook(asks=(resting,)), incoming)

    assert len(result.fills) == 1
    assert result.fills[0].quantity == 5
    assert result.book.asks == ()  # resting order fully consumed
    assert result.resting is not None
    assert result.resting.remaining_quantity == 5
    assert result.book.bids == (result.resting,)


def test_resting_order_partially_fills_and_stays_in_the_book():
    resting = limit_order(SELL, 100, 10, sequence=1)
    incoming = limit_order(BUY, 100, 4, sequence=2)  # wants less than is offered

    result = match(OrderBook(asks=(resting,)), incoming)

    assert result.fills[0].quantity == 4
    assert result.resting is None  # incoming fully filled, nothing to rest
    assert len(result.book.asks) == 1
    assert result.book.asks[0].order_id == resting.order_id
    assert result.book.asks[0].remaining_quantity == 6


def test_exact_quantity_match_leaves_neither_order_in_the_book():
    resting = limit_order(SELL, 100, 10, sequence=1)
    incoming = limit_order(BUY, 100, 10, sequence=2)

    result = match(OrderBook(asks=(resting,)), incoming)

    assert result.fills[0].quantity == 10
    assert result.resting is None
    assert result.book == OrderBook()


# --------------------------------------------------------------------------
# market order sweeping multiple price levels
# --------------------------------------------------------------------------

def test_market_order_sweeps_multiple_price_levels_in_price_order():
    level1 = limit_order(SELL, 100, 5, sequence=1)
    level2 = limit_order(SELL, 101, 5, sequence=2)
    level3 = limit_order(SELL, 102, 5, sequence=3)
    book = OrderBook(asks=(level3, level1, level2))  # deliberately unsorted

    incoming = market_order(BUY, 12)  # needs all of level1, all of level2, part of level3
    result = match(book, incoming)

    assert [f.price_minor for f in result.fills] == [100, 101, 102]
    assert [f.quantity for f in result.fills] == [5, 5, 2]
    assert result.resting is None  # market orders never rest
    assert len(result.book.asks) == 1
    assert result.book.asks[0].order_id == level3.order_id
    assert result.book.asks[0].remaining_quantity == 3


def test_market_order_that_exhausts_the_book_drops_its_remainder():
    resting = limit_order(SELL, 100, 5, sequence=1)
    incoming = market_order(BUY, 20)  # far more than the book can offer

    result = match(OrderBook(asks=(resting,)), incoming)

    assert len(result.fills) == 1
    assert result.fills[0].quantity == 5
    assert result.resting is None  # the unfilled 15 is simply gone, not an error
    assert result.book == OrderBook()


def test_market_sell_sweeps_bids_from_highest_price_down():
    high = limit_order(BUY, 110, 3, sequence=1)
    mid = limit_order(BUY, 105, 3, sequence=2)
    low = limit_order(BUY, 100, 3, sequence=3)
    book = OrderBook(bids=(low, mid, high))

    incoming = market_order(SELL, 7)
    result = match(book, incoming)

    assert [f.price_minor for f in result.fills] == [110, 105, 100]
    assert [f.quantity for f in result.fills] == [3, 3, 1]
    assert result.book.bids[0].order_id == low.order_id
    assert result.book.bids[0].remaining_quantity == 2


# --------------------------------------------------------------------------
# a limit order that's aggressive enough to also sweep multiple levels
# --------------------------------------------------------------------------

def test_aggressive_limit_order_sweeps_levels_up_to_its_own_limit():
    level1 = limit_order(SELL, 100, 5, sequence=1)
    level2 = limit_order(SELL, 101, 5, sequence=2)
    level3 = limit_order(SELL, 105, 5, sequence=3)  # beyond incoming's limit
    book = OrderBook(asks=(level1, level2, level3))

    incoming = limit_order(BUY, 101, 10, sequence=4)  # willing to pay up to 101, not 105
    result = match(book, incoming)

    assert [f.price_minor for f in result.fills] == [100, 101]
    assert result.resting is None  # exactly filled by levels 1 and 2
    assert result.book.asks == (level3,)


# --------------------------------------------------------------------------
# self-match — the matcher is ownership-blind; see ADR 0008 Decision 3
# --------------------------------------------------------------------------

def test_self_match_produces_a_normal_fill():
    """
    Nothing about BookOrder carries an owner — the matcher can't tell a
    self-match from any other match, and per ADR 0008 it deliberately
    doesn't try to. This test exists to prove that explicitly: two orders
    that WOULD belong to the same owner (tracked only by the caller, not by
    this module) still cross normally.
    """
    resting = limit_order(SELL, 100, 10, sequence=1)
    incoming = limit_order(BUY, 100, 10, sequence=2)  # "same owner" is a caller-side fact

    result = match(OrderBook(asks=(resting,)), incoming)

    assert len(result.fills) == 1
    assert result.fills[0].buy_order_id == incoming.order_id
    assert result.fills[0].sell_order_id == resting.order_id
    assert result.fills[0].quantity == 10


# --------------------------------------------------------------------------
# invariant: a market order can never be found resting in the book
# --------------------------------------------------------------------------

def test_a_market_order_resting_in_the_book_is_rejected_outright():
    """
    This should never happen if the rest of the system respects rule 5, but
    match() checks it explicitly rather than crashing confusingly inside a
    sort comparator that tries to compare None against an int.
    """
    broken_book = OrderBook(asks=(market_order(SELL, 5),))
    with pytest.raises(ValueError, match="market order can never rest"):
        match(broken_book, limit_order(BUY, 100, 1, sequence=1))
