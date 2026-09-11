from datetime import UTC, datetime

import pytest

from code_review_agent.domain.task.models import (
    Checkpoint,
    CheckpointKind,
    ControlState,
    DeliveryState,
    InputBinding,
    Phase,
    ResultState,
    TaskSpec,
)


def spec() -> TaskSpec:
    return TaskSpec(
        spec_id="spec-1",
        input_intent="diff:abc",
        provider_id="provider",
        provider_version="1",
        model_id="model",
        provider_origin="https://api.example.test",
        ruleset_id="rules",
        ruleset_version="1",
        tools=("tool@1",),
        security_policy_id="security",
        security_policy_version=1,
        config_digest="config",
        credential_alias="shared",
        budget_account_id="budget-1",
    )


def test_task_spec_is_immutable_and_has_fixed_condition_digest() -> None:
    value = spec()

    assert value.fixed_conditions_digest
    with pytest.raises(AttributeError):
        value.model_id = "other"  # type: ignore[misc]


def test_input_binding_can_only_represent_one_fixed_version() -> None:
    binding = InputBinding(
        binding_id="binding-1",
        task_id="task-1",
        input_type="plain_diff",
        object_identity="diff",
        base_sha=None,
        head_sha=None,
        content_digest="digest",
        completeness_digest="complete",
        changeset_ref="changeset",
    )
    assert binding.task_id == "task-1"
    with pytest.raises(ValueError):
        InputBinding(
            binding_id="binding-1",
            task_id="task-2",
            input_type="plain_diff",
            object_identity="diff",
            base_sha=None,
            head_sha=None,
            content_digest="digest",
            completeness_digest="complete",
            changeset_ref="changeset",
        )


def test_checkpoint_requires_predecessor_for_non_initial_sequence() -> None:
    with pytest.raises(ValueError):
        Checkpoint(
            checkpoint_id="checkpoint-2",
            task_id="task-1",
            sequence=2,
            kind=CheckpointKind.REVIEW_UNIT_COMPLETED,
            phase=Phase.REVIEWING,
            scope_type="work_unit",
            scope_id="unit-1",
            predecessor_id=None,
            budget_ledger_version=1,
            trace_start_sequence=1,
            trace_end_sequence=2,
            created_task_version=2,
            created_at=datetime.now(UTC),
        )


def test_state_enums_are_orthogonal() -> None:
    assert ControlState.PAUSED.value == "paused"
    assert Phase.REVIEWING.value == "reviewing"
    assert ResultState.UNKNOWN.value == "unknown"
    assert DeliveryState.FAILED.value == "failed"
