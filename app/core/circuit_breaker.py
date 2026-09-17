"""
A circuit breaker, as pure state-machine logic with no I/O of its own.

See docs/adr/0007-observability.md Decision 2 for why this exists instead of
a naive retry loop. Kept dependency-free (no Redis, no Kafka) and the clock
is injectable specifically so state transitions are unit-testable without
sleeping a real 30 seconds — see tests/test_circuit_breaker.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum


class CircuitState(str, Enum):
    """
    str so a metric label or a log line can use `breaker.state.value`
    directly instead of a case statement translating an int.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised by a caller's guarded operation when the breaker refuses it."""


class CircuitBreaker:
    """
    Three states, four transitions:

        CLOSED  --[consecutive failures reach failure_threshold]--> OPEN
        OPEN    --[recovery_timeout_s elapses]--------------------> HALF_OPEN
        HALF_OPEN --[trial succeeds]--------------------------------> CLOSED
        HALF_OPEN --[trial fails]-----------------------------------> OPEN

    This class only tracks state — it never makes the guarded call itself.
    The caller is expected to:

        if not breaker.allow_request():
            raise CircuitOpenError(...)
        try:
            result = await do_the_real_thing()
        except Exception:
            breaker.record_failure()
            raise
        else:
            breaker.record_success()
            return result
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        recovery_timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        # OPEN -> HALF_OPEN is time-driven, not event-driven: nothing calls
        # the breaker while the broker is down, so this promotion has to
        # happen lazily, on the next read, rather than waiting for some
        # background timer that doesn't exist here.
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self._recovery_timeout_s:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        """True in CLOSED and HALF_OPEN (the trial is itself an allowed request)."""
        return self.state is not CircuitState.OPEN

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        if self.state is CircuitState.HALF_OPEN:
            # A failed trial means the dependency isn't back. Reopen and
            # restart the cooldown rather than folding this into the
            # CLOSED-state failure count below — those are different
            # questions ("has it failed enough to distrust it?" vs "did the
            # one thing we just tried to confirm recovery with fail?").
            self._trip()
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._consecutive_failures = self._failure_threshold
