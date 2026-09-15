import uuid

from app.services.reconciliation import _find_drift

A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def test_matching_rows_report_no_drift():
    assert _find_drift([(A, 500, 500), (B, 0, 0)]) == []


def test_mismatched_row_is_reported():
    drift = _find_drift([(A, 500, 500), (B, 100, 70)])
    assert len(drift) == 1
    assert drift[0].account_id == B
    assert drift[0].recorded == 100
    assert drift[0].derived == 70


def test_diff_is_derived_minus_recorded():
    """Positive diff: the ledger has more than the cache thinks. Negative: less."""
    over = _find_drift([(A, 100, 70)])[0]
    under = _find_drift([(A, 70, 100)])[0]
    assert over.diff == -30
    assert under.diff == 30


def test_empty_input_reports_no_drift():
    assert _find_drift([]) == []
