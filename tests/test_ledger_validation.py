import uuid

import pytest

from app.services.ledger import (
    Posting,
    UnbalancedTransaction,
    validate_currency_balance,
    validate_postings,
)

A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
C = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
D = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


def test_balanced_pair_is_accepted():
    merged = validate_postings([Posting(A, -500), Posting(B, +500)])
    assert merged == {A: -500, B: +500}


def test_multi_leg_transaction_with_a_fee():
    """Payer 1000 out; payee gets 970, platform keeps 30. Still sums to zero."""
    merged = validate_postings(
        [Posting(A, -1000), Posting(B, +970), Posting(C, +30)]
    )
    assert sum(merged.values()) == 0
    assert merged[C] == 30


def test_duplicate_accounts_are_merged():
    """Locking the same account twice is how you deadlock against yourself."""
    merged = validate_postings(
        [Posting(A, -300), Posting(A, -200), Posting(B, +500)]
    )
    assert merged == {A: -500, B: +500}


def test_unbalanced_is_rejected():
    with pytest.raises(UnbalancedTransaction, match="sum to 100"):
        validate_postings([Posting(A, -400), Posting(B, +500)])


def test_zero_amount_posting_is_rejected():
    with pytest.raises(UnbalancedTransaction):
        validate_postings([Posting(A, 0), Posting(B, 0)])


def test_single_posting_is_rejected():
    with pytest.raises(UnbalancedTransaction):
        validate_postings([Posting(A, 0)])


def test_self_transfer_collapses_to_nothing_and_is_rejected():
    """A -> A nets to zero. There is no movement, so there is no transaction."""
    with pytest.raises(UnbalancedTransaction, match="fewer than two"):
        validate_postings([Posting(A, -500), Posting(A, +500)])


def test_large_amounts_stay_exact():
    """BIGINT minor units, not floats. This is the whole reason for that rule."""
    big = 9_223_372_036_854_775  # well inside int64, absurd in float64
    merged = validate_postings([Posting(A, -big), Posting(B, +big)])
    assert sum(merged.values()) == 0


# --------------------------------------------------------------------------
# validate_currency_balance — ADR 0008 Decision 4: a trade's cash leg and
# position leg settle in ONE transaction spanning two currencies (INR and,
# here, "AAPL" standing in for a position account's instrument symbol —
# see ADR 0008 on `currency` as "unit of account"). These are the tests for
# the generalized rule itself, independent of trading — a database is never
# involved, same as validate_postings() above.
# --------------------------------------------------------------------------

def test_single_currency_still_just_needs_to_net_to_zero():
    """The pre-M6 case, unchanged: one currency, sums to zero, passes."""
    validate_currency_balance({A: -500, B: +500}, {A: "INR", B: "INR"})


def test_single_currency_that_does_not_net_to_zero_is_rejected():
    with pytest.raises(UnbalancedTransaction, match="INR"):
        validate_currency_balance({A: -500, B: +400}, {A: "INR", B: "INR"})


def test_a_balanced_cash_leg_and_position_leg_together_is_accepted():
    """
    The exact shape a trade settles as: buyer's cash -P*Q, seller's cash
    +P*Q (nets to zero in INR), buyer's position +Q, seller's position -Q
    (nets to zero in AAPL) — two currencies, each independently balanced,
    in one call.
    """
    validate_currency_balance(
        {A: -50_000, B: +50_000, C: +10, D: -10},
        {A: "INR", B: "INR", C: "AAPL", D: "AAPL"},
    )


def test_cash_balanced_but_position_leg_wrong_is_rejected():
    """
    A bug that got the cash leg right but the share quantity wrong (e.g.
    credited the buyer 9 shares for a 10-share trade) must be caught even
    though the OTHER currency in the same transaction is perfectly balanced
    — this is exactly what a single whole-transaction sum could never catch,
    since -50_000 + 50_000 + 9 - 10 happens to be -1, which fails anyway,
    but a same-magnitude slip (say +11 instead of +9) would net the WHOLE
    transaction to zero by coincidence while still being wrong per-currency.
    """
    with pytest.raises(UnbalancedTransaction, match="AAPL"):
        validate_currency_balance(
            {A: -50_000, B: +50_000, C: +11, D: -10},
            {A: "INR", B: "INR", C: "AAPL", D: "AAPL"},
        )


def test_whole_transaction_sums_to_zero_by_coincidence_but_per_currency_fails():
    """
    The case validate_postings()'s OLD single-sum check would have wrongly
    accepted: -100 (INR) + 100 (AAPL) sums to 0 as one undifferentiated
    total, but neither currency nets to zero on its own. This is the
    concrete reason the per-currency check has to exist as its own pass,
    not just "trust the total."
    """
    with pytest.raises(UnbalancedTransaction):
        validate_currency_balance({A: -100, B: +100}, {A: "INR", B: "AAPL"})


def test_two_currency_groups_are_checked_independently_of_each_other():
    validate_currency_balance(
        {A: -100, B: +100, C: -5, D: +5},
        {A: "INR", B: "INR", C: "USD", D: "USD"},
    )
