import pytest

from code_review_agent.domain.task.models import ControlState, Phase
from code_review_agent.domain.task.state_machine import (
    advance_phase,
    validate_control_transition,
)


def test_phase_only_moves_forward() -> None:
    assert advance_phase(Phase.CREATED, Phase.INPUT_ACQUIRED) is Phase.INPUT_ACQUIRED
    with pytest.raises(ValueError):
        advance_phase(Phase.PLANNED, Phase.INPUT_NORMALIZED)


def test_control_transition_rejects_resuming_terminated_task() -> None:
    with pytest.raises(ValueError):
        validate_control_transition(ControlState.TERMINATED, ControlState.RUNNING)
    validate_control_transition(ControlState.READY, ControlState.RUNNING)
