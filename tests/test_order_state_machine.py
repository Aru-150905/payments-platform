import pytest

from app.db.models import OrderStatus as S
from app.domain.order_state_machine import (
    ALLOWED,
    TERMINAL,
    IllegalOrderTransition,
    assert_can_transition,
)


def test_resting_to_partially_filled_is_legal():
    assert_can_transition(S.OPEN, S.PARTIALLY_FILLED)


def test_resting_to_fully_filled_is_legal():
    assert_can_transition(S.OPEN, S.FILLED)


def test_open_to_cancelled_is_legal():
    assert_can_transition(S.OPEN, S.CANCELLED)


def test_a_second_partial_fill_is_legal():
    """
    The one deliberate difference from the payment state machine: repeating
    PARTIALLY_FILLED -> PARTIALLY_FILLED represents a second fill against an
    order too large for one match, not a retry — see this module's
    docstring in app/domain/order_state_machine.py.
    """
    assert_can_transition(S.PARTIALLY_FILLED, S.PARTIALLY_FILLED)


def test_partially_filled_can_finish_filling():
    assert_can_transition(S.PARTIALLY_FILLED, S.FILLED)


def test_partially_filled_can_be_cancelled():
    """Cancelling the unfilled remainder of an order already worked in part."""
    assert_can_transition(S.PARTIALLY_FILLED, S.CANCELLED)


@pytest.mark.parametrize(
    "current,target",
    [
        (S.OPEN, S.OPEN),                    # unlike PARTIALLY_FILLED, OPEN does not repeat
        (S.FILLED, S.OPEN),                  # terminal
        (S.FILLED, S.PARTIALLY_FILLED),      # terminal
        (S.FILLED, S.CANCELLED),             # terminal — fully filled, nothing left to cancel
        (S.CANCELLED, S.OPEN),               # terminal
        (S.CANCELLED, S.FILLED),             # terminal
        (S.CANCELLED, S.PARTIALLY_FILLED),   # terminal
    ],
)
def test_illegal_transitions_raise(current, target):
    with pytest.raises(IllegalOrderTransition):
        assert_can_transition(current, target)


def test_every_status_has_an_explicit_entry():
    """A missing key is an oversight; an empty set is a decision."""
    assert set(ALLOWED) == set(S)


def test_terminal_states_are_the_expected_ones():
    assert TERMINAL == {S.FILLED, S.CANCELLED}


def test_no_transition_escapes_a_terminal_state():
    for state in TERMINAL:
        for target in S:
            with pytest.raises(IllegalOrderTransition):
                assert_can_transition(state, target)
