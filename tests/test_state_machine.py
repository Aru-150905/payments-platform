import pytest

from app.db.models import PaymentStatus as S
from app.domain.state_machine import ALLOWED, TERMINAL, IllegalTransition, assert_can_transition


def test_happy_path_is_legal():
    assert_can_transition(S.INITIATED, S.AUTHORIZED)
    assert_can_transition(S.AUTHORIZED, S.CAPTURED)
    assert_can_transition(S.CAPTURED, S.REFUNDED)


@pytest.mark.parametrize(
    "current,target",
    [
        (S.INITIATED, S.CAPTURED),   # cannot skip authorization
        (S.CAPTURED, S.VOIDED),      # money already moved; use a refund
        (S.VOIDED, S.CAPTURED),      # terminal
        (S.REFUNDED, S.CAPTURED),    # terminal
        (S.CAPTURED, S.CAPTURED),    # a second capture is never a no-op
    ],
)
def test_illegal_transitions_raise(current, target):
    with pytest.raises(IllegalTransition):
        assert_can_transition(current, target)


def test_every_status_has_an_explicit_entry():
    """A missing key is an oversight; an empty set is a decision."""
    assert set(ALLOWED) == set(S)


def test_terminal_states_are_the_expected_ones():
    assert TERMINAL == {S.VOIDED, S.FAILED, S.REFUNDED}


def test_no_transition_escapes_a_terminal_state():
    for state in TERMINAL:
        for target in S:
            with pytest.raises(IllegalTransition):
                assert_can_transition(state, target)
