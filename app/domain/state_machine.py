"""
The payment state machine.

Why this file exists as a separate thing instead of `if status == ...` checks
scattered through the service layer: the set of legal transitions is a business
rule that auditors, tests and future-you all need to read in one place. When it
lives in ten if-statements, someone eventually adds an eleventh that allows
capturing a voided payment.
"""

from __future__ import annotations

from app.db.models import PaymentStatus

S = PaymentStatus

# from -> set of allowed next states
ALLOWED: dict[PaymentStatus, set[PaymentStatus]] = {
    S.INITIATED:  {S.AUTHORIZED, S.FAILED},
    S.AUTHORIZED: {S.CAPTURED, S.VOIDED, S.FAILED},
    S.CAPTURED:   {S.REFUNDED},
    # Terminal states. Empty set, not a missing key — an explicit "nothing
    # follows this" is a statement; a missing key is an oversight.
    S.VOIDED:     set(),
    S.FAILED:     set(),
    S.REFUNDED:   set(),
}

TERMINAL = {s for s, nxt in ALLOWED.items() if not nxt}


class IllegalTransition(Exception):
    def __init__(self, current: PaymentStatus, target: PaymentStatus):
        self.current = current
        self.target = target
        super().__init__(f"cannot move payment from {current.value} to {target.value}")


def assert_can_transition(current: PaymentStatus, target: PaymentStatus) -> None:
    """
    Raise unless `current -> target` is legal.

    Note it does NOT treat `current == target` as a harmless no-op. A second
    capture request is either a retry (which idempotency should have caught
    upstream) or a bug, and silently succeeding hides both.
    """
    if target not in ALLOWED[current]:
        raise IllegalTransition(current, target)
