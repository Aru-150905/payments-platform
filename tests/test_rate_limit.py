"""
Pure-logic tests for app/api/rate_limit.py — no Redis. Covers the sliding
window's boundary arithmetic (the same rule the Lua script re-implements
atomically inside Redis) and the exact shape of the 429 response, since a
wrong Retry-After or a body a client can't parse is as real a bug as an
off-by-one in the limit itself.
"""

import json

from app.api.auth import verify_api_key
from app.api.rate_limit import (
    count_in_window,
    is_allowed,
    rate_limited_response,
    window_cutoff,
)

# --------------------------------------------------------------------------
# window_cutoff
# --------------------------------------------------------------------------

def test_window_cutoff_is_now_minus_window():
    assert window_cutoff(now_ms=100_000, window_ms=60_000) == 40_000


def test_window_cutoff_with_zero_now_can_go_negative():
    """No clamping — a caller passing now < window (only possible with a bad
    clock) gets an honest negative cutoff rather than a silently wrong zero."""
    assert window_cutoff(now_ms=100, window_ms=60_000) == -59_900


# --------------------------------------------------------------------------
# count_in_window — the exact boundary the Lua script must match
# --------------------------------------------------------------------------

def test_event_exactly_at_the_cutoff_is_counted():
    # cutoff = 100_000 - 60_000 = 40_000
    assert count_in_window([40_000], now_ms=100_000, window_ms=60_000) == 1


def test_event_one_ms_before_the_cutoff_is_excluded():
    assert count_in_window([39_999], now_ms=100_000, window_ms=60_000) == 0


def test_event_at_now_is_counted():
    assert count_in_window([100_000], now_ms=100_000, window_ms=60_000) == 1


def test_mixed_events_only_in_window_ones_counted():
    timestamps = [10_000, 39_999, 40_000, 70_000, 100_000, 100_001]
    # in-window: 40_000, 70_000, 100_000 (100_001 is in the future of `now`,
    # but still >= cutoff — count_in_window doesn't second-guess a caller's
    # own `now`, it only enforces the lower bound).
    assert count_in_window(timestamps, now_ms=100_000, window_ms=60_000) == 4


def test_empty_window_counts_zero():
    assert count_in_window([], now_ms=100_000, window_ms=60_000) == 0


# --------------------------------------------------------------------------
# is_allowed
# --------------------------------------------------------------------------

def test_count_below_limit_is_allowed():
    assert is_allowed(59, limit=60) is True


def test_count_equal_to_limit_is_rejected():
    """This is the line the whole limiter exists to hold: at the limit, the
    NEXT request (which would make count == limit) must be refused, not the
    one that reaches it."""
    assert is_allowed(60, limit=60) is False


def test_count_above_limit_is_rejected():
    assert is_allowed(61, limit=60) is False


def test_zero_count_against_zero_limit_is_rejected():
    """A limit of 0 must reject everything, not divide-by-zero or default-allow."""
    assert is_allowed(0, limit=0) is False


# --------------------------------------------------------------------------
# rate_limited_response — exact status, headers, and body shape
# --------------------------------------------------------------------------

def test_response_status_is_429():
    assert rate_limited_response(retry_after_ms=1000).status_code == 429


def test_response_body_has_machine_readable_error_and_precise_retry_after():
    response = rate_limited_response(retry_after_ms=1500)
    body = json.loads(response.body)
    assert body == {"error": "rate_limited", "retry_after_ms": 1500}


def test_retry_after_header_rounds_up_to_whole_seconds():
    """Retry-After is defined in seconds. Rounding DOWN would let a client
    that obeys it literally retry before the window has actually cleared."""
    assert rate_limited_response(retry_after_ms=1).headers["retry-after"] == "1"
    assert rate_limited_response(retry_after_ms=1000).headers["retry-after"] == "1"
    assert rate_limited_response(retry_after_ms=1001).headers["retry-after"] == "2"


def test_retry_after_header_is_never_zero_or_negative():
    """A retry_after_ms of 0 (or a small clock-skew negative) must still tell
    the client to wait at least 1s, not "retry immediately" or "retry in the
    past" — either of which would defeat the limiter it's attached to."""
    assert rate_limited_response(retry_after_ms=0).headers["retry-after"] == "1"
    assert rate_limited_response(retry_after_ms=-50).headers["retry-after"] == "1"


# --------------------------------------------------------------------------
# API key comparison (auth.py) — small enough to belong alongside these
# --------------------------------------------------------------------------

def test_matching_api_key_is_accepted():
    assert verify_api_key("secret-value", "secret-value") is True


def test_wrong_api_key_is_rejected():
    assert verify_api_key("guessed-value", "secret-value") is False


def test_empty_provided_key_is_rejected():
    assert verify_api_key("", "secret-value") is False


def test_prefix_match_is_not_enough():
    """Guards against a naive comparison that could short-circuit true on a
    partial/prefix match instead of the full value."""
    assert verify_api_key("secret", "secret-value") is False
