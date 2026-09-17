"""
The order state machine.

Same reasoning as app/domain/state_machine.py (the payment one): the set of
legal transitions is a business rule that belongs in one place a reviewer
can read end to end, not scattered across if-statements in the service
layer.

One real difference from the payment state machine, worth calling out
explicitly because it looks like a copy-paste mistake if you don't know why
it's there: PARTIALLY_FILLED -> PARTIALLY_FILLED is LEGAL here.
assert_can_transition() in the payment machine treats current == target as
always illegal — a repeated capture is a retry or a bug, never a real second
event. An order is different: it can receive many partial fills over its
life, and each one is a genuine, separate event (the quantity filled
changes even though the status label doesn't move). Treating that as
"illegal" would make a normal, expected sequence of fills impossible to
represent.
"""

from __future__ import annotations

from app.db.models import OrderStatus

S = OrderStatus

# from -> set of allowed next states
ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    S.OPEN: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED},
    # Includes itself — see this module's docstring. A second (or third...)
    # partial fill is not a retry of the first; it is the normal way an
    # order that's too big for one match gets worked over time.
    S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED},
    # Terminal states. Empty set, not a missing key — see the payment state
    # machine's identical convention: an explicit "nothing follows this" is
    # a statement, a missing key is an oversight.
    S.FILLED: set(),
    S.CANCELLED: set(),
}

TERMINAL = {s for s, nxt in ALLOWED.items() if not nxt}


class IllegalOrderTransition(Exception):
    def __init__(self, current: OrderStatus, target: OrderStatus):
        self.current = current
        self.target = target
        super().__init__(f"cannot move order from {current.value} to {target.value}")


def assert_can_transition(current: OrderStatus, target: OrderStatus) -> None:
    """Raise unless `current -> target` is legal."""
    if target not in ALLOWED[current]:
        raise IllegalOrderTransition(current, target)
