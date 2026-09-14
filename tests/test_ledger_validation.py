import uuid

import pytest

from app.services.ledger import Posting, UnbalancedTransaction, validate_postings

A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
C = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


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
