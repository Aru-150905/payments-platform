"""
Pure state-machine tests for app/core/circuit_breaker.py — no Kafka, no
network. A FakeClock stands in for time.monotonic so recovery-timeout
transitions are exact and instant rather than requiring a real sleep.

Biased toward the rejection/transition paths per project history: five of
six bugs so far have been in error, retry, concurrency, or reporting paths,
never the happy path.
"""

from app.core.circuit_breaker import CircuitBreaker, CircuitState


class FakeClock:
    """A clock that only moves when told to — makes boundary time asserts exact."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(*, failure_threshold=3, recovery_timeout_s=10.0, clock=None):
    clock = clock or FakeClock()
    return CircuitBreaker(
        failure_threshold=failure_threshold,
        recovery_timeout_s=recovery_timeout_s,
        clock=clock,
    ), clock


# --------------------------------------------------------------------------
# CLOSED state
# --------------------------------------------------------------------------

def test_starts_closed_and_allows_requests():
    breaker, _ = make_breaker()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow_request() is True


def test_failures_below_threshold_stay_closed():
    breaker, _ = make_breaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow_request() is True


def test_a_success_resets_the_failure_count():
    """
    2 failures, a success, then 2 more failures must NOT trip a
    threshold=3 breaker — the success has to erase the earlier count, not
    just pause it. Catches an implementation that only zeroes the counter on
    the transition out of OPEN instead of on every record_success().
    """
    breaker, _ = make_breaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED


# --------------------------------------------------------------------------
# CLOSED -> OPEN
# --------------------------------------------------------------------------

def test_trips_open_exactly_at_the_failure_threshold():
    breaker, _ = make_breaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()  # the 3rd consecutive failure
    assert breaker.state == CircuitState.OPEN


def test_open_breaker_rejects_requests():
    breaker, _ = make_breaker(failure_threshold=1)
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False


# --------------------------------------------------------------------------
# OPEN -> HALF_OPEN (time-driven)
# --------------------------------------------------------------------------

def test_stays_open_before_recovery_timeout_elapses():
    breaker, clock = make_breaker(failure_threshold=1, recovery_timeout_s=10.0)
    breaker.record_failure()
    clock.advance(9.999)
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False


def test_becomes_half_open_at_exactly_the_recovery_timeout():
    """Boundary is inclusive: `elapsed >= recovery_timeout_s` promotes."""
    breaker, clock = make_breaker(failure_threshold=1, recovery_timeout_s=10.0)
    breaker.record_failure()
    clock.advance(10.0)
    assert breaker.state == CircuitState.HALF_OPEN


def test_becomes_half_open_after_recovery_timeout_elapses():
    breaker, clock = make_breaker(failure_threshold=1, recovery_timeout_s=10.0)
    breaker.record_failure()
    clock.advance(15.0)
    assert breaker.state == CircuitState.HALF_OPEN


def test_half_open_allows_the_trial_request():
    breaker, clock = make_breaker(failure_threshold=1, recovery_timeout_s=10.0)
    breaker.record_failure()
    clock.advance(10.0)
    assert breaker.allow_request() is True


# --------------------------------------------------------------------------
# HALF_OPEN -> CLOSED / OPEN
# --------------------------------------------------------------------------

def test_half_open_success_closes_the_breaker():
    breaker, clock = make_breaker(failure_threshold=1, recovery_timeout_s=10.0)
    breaker.record_failure()
    clock.advance(10.0)
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow_request() is True


def test_half_open_failure_reopens_immediately():
    """
    A single failed trial reopens the breaker — it must NOT need to
    re-accumulate failure_threshold failures again, since only one request
    is ever attempted during a half-open trial in this codebase's usage
    (the relay publishes sequentially).
    """
    breaker, clock = make_breaker(failure_threshold=3, recovery_timeout_s=10.0)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    clock.advance(10.0)
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_failure()  # the one trial, and it fails
    assert breaker.state == CircuitState.OPEN


def test_half_open_failure_restarts_the_recovery_cooldown():
    breaker, clock = make_breaker(failure_threshold=1, recovery_timeout_s=10.0)
    breaker.record_failure()
    clock.advance(10.0)
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_failure()  # reopens
    clock.advance(9.999)
    assert breaker.state == CircuitState.OPEN  # cooldown restarted, not reused

    clock.advance(0.001)
    assert breaker.state == CircuitState.HALF_OPEN


# --------------------------------------------------------------------------
# exact values other code/metrics will depend on
# --------------------------------------------------------------------------

def test_state_values_are_the_exact_strings_other_code_matches_on():
    assert CircuitState.CLOSED.value == "closed"
    assert CircuitState.OPEN.value == "open"
    assert CircuitState.HALF_OPEN.value == "half_open"
