"""Task lifecycle transition rules."""

from __future__ import annotations

from code_review_agent.domain.task.models import ControlState, Phase

_PHASE_ORDER = {
    Phase.CREATED: 0,
    Phase.INPUT_ACQUIRED: 1,
    Phase.INPUT_NORMALIZED: 2,
    Phase.PLANNED: 3,
    Phase.REVIEWING: 4,
    Phase.CONSOLIDATING: 5,
    Phase.RESULT_FINALIZED: 6,
}


def advance_phase(current: Phase, target: Phase) -> Phase:
    if _PHASE_ORDER[target] <= _PHASE_ORDER[current]:
        raise ValueError("illegal_state_transition")
    return target


def validate_control_transition(current: ControlState, target: ControlState) -> None:
    if current is ControlState.TERMINATED and target is not ControlState.TERMINATED:
        raise ValueError("illegal_state_transition")
    allowed = {
        ControlState.READY: {
            ControlState.RUNNING,
            ControlState.PAUSED,
            ControlState.TERMINATED,
        },
        ControlState.RUNNING: {ControlState.PAUSED, ControlState.TERMINATED},
        ControlState.PAUSED: {ControlState.RUNNING, ControlState.TERMINATED},
        ControlState.TERMINATED: {ControlState.TERMINATED},
    }
    if target not in allowed[current]:
        raise ValueError("illegal_state_transition")
