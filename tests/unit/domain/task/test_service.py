from datetime import UTC, datetime, timedelta

import pytest

from code_review_agent.domain.common.time import FixedClock
from code_review_agent.domain.task.models import (
    Checkpoint,
    CheckpointKind,
    ControlState,
    Phase,
    ResultState,
    TaskSpec,
)
from code_review_agent.domain.task.service import TaskService


def make_spec() -> TaskSpec:
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


def checkpoint(
    task_id: str, sequence: int, predecessor_id: str | None = None
) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=f"checkpoint-{sequence}",
        task_id=task_id,
        sequence=sequence,
        kind=CheckpointKind.INPUT_ACQUIRED,
        phase=Phase.INPUT_ACQUIRED,
        scope_type="input",
        scope_id="input",
        predecessor_id=predecessor_id,
        budget_ledger_version=1,
        trace_start_sequence=1,
        trace_end_sequence=sequence,
        created_task_version=sequence,
        created_at=datetime.now(UTC),
    )


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


def test_acquire_renew_and_fence_execution_lease() -> None:
    now = datetime(2026, 9, 11, tzinfo=UTC)
    service = TaskService(clock=FixedClock(now))
    task = service.create_task(make_spec())
    permit = service.acquire_execution(task.task_id, owner_id="owner-1")

    assert service.get(task.task_id).control_state is ControlState.RUNNING
    with pytest.raises(ValueError):
        service.acquire_execution(task.task_id, owner_id="owner-2")
    renewed = service.renew_lease(task.task_id, permit.lease_id, permit.fencing_token)
    assert renewed.lease_revision == 2
    assert service.get(task.task_id).version == permit.task_version
    with pytest.raises(ValueError):
        service.release_execution(
            task.task_id, permit.lease_id, permit.fencing_token - 1
        )


def test_phase_completion_requires_expected_version_lease_and_checkpoint_chain() -> (
    None
):
    service = TaskService(clock=FixedClock(datetime(2026, 9, 11, tzinfo=UTC)))
    task = service.create_task(make_spec())
    permit = service.acquire_execution(task.task_id, owner_id="owner-1")
    updated = service.complete_phase(
        task.task_id,
        expected_version=permit.task_version,
        lease_id=permit.lease_id,
        fencing_token=permit.fencing_token,
        target_phase=Phase.INPUT_ACQUIRED,
        checkpoint=checkpoint(task.task_id, 1),
    )
    assert updated.phase is Phase.INPUT_ACQUIRED
    with pytest.raises(ValueError):
        service.complete_phase(
            task.task_id,
            expected_version=permit.task_version,
            lease_id=permit.lease_id,
            fencing_token=permit.fencing_token,
            target_phase=Phase.INPUT_NORMALIZED,
            checkpoint=checkpoint(task.task_id, 2, "checkpoint-1"),
        )


def test_pause_resume_and_unknown_requires_explicit_confirmation() -> None:
    now = datetime(2026, 9, 11, tzinfo=UTC)
    service = TaskService(clock=FixedClock(now))
    task = service.create_task(make_spec())
    permit = service.acquire_execution(task.task_id, owner_id="owner-1")
    service.pause(
        task.task_id,
        expected_version=permit.task_version,
        lease_id=permit.lease_id,
        fencing_token=permit.fencing_token,
        reason="external_result_unknown",
        result_state=ResultState.UNKNOWN,
    )
    with pytest.raises(ValueError):
        service.resume(task.task_id, owner_id="owner-2")
    resumed = service.resume(
        task.task_id, owner_id="owner-2", confirm_unknown_retry=True
    )
    assert resumed.fencing_token > permit.fencing_token
    assert service.get(task.task_id).control_state is ControlState.RUNNING


def test_expired_lease_is_recoverable_but_clock_queries_do_not_mutate_task() -> None:
    now = datetime(2026, 9, 11, tzinfo=UTC)
    clock = MutableClock(now)
    service = TaskService(clock=clock)
    task = service.create_task(make_spec())
    service.acquire_execution(task.task_id, owner_id="owner-1")
    before = service.get(task.task_id).version
    clock.current = now + timedelta(seconds=121)
    status = service.recoverability(task.task_id)
    assert status == "recoverable"
    assert service.get(task.task_id).version == before


def test_input_binding_is_written_once_after_normalization() -> None:
    service = TaskService(clock=FixedClock(datetime(2026, 9, 11, tzinfo=UTC)))
    task = service.create_task(make_spec())
    permit = service.acquire_execution(task.task_id, owner_id="owner-1")
    first = service.complete_phase(
        task.task_id,
        expected_version=permit.task_version,
        lease_id=permit.lease_id,
        fencing_token=permit.fencing_token,
        target_phase=Phase.INPUT_ACQUIRED,
        checkpoint=checkpoint(task.task_id, 1),
    )
    second = service.complete_phase(
        task.task_id,
        expected_version=first.version,
        lease_id=permit.lease_id,
        fencing_token=permit.fencing_token,
        target_phase=Phase.INPUT_NORMALIZED,
        checkpoint=Checkpoint(
            checkpoint_id="checkpoint-2",
            task_id=task.task_id,
            sequence=2,
            kind=CheckpointKind.INPUT_NORMALIZED,
            phase=Phase.INPUT_NORMALIZED,
            scope_type="input",
            scope_id="input",
            predecessor_id="checkpoint-1",
            budget_ledger_version=1,
            trace_start_sequence=1,
            trace_end_sequence=2,
            created_task_version=first.version,
            created_at=datetime.now(UTC),
        ),
    )
    assert second.phase is Phase.INPUT_NORMALIZED
    from code_review_agent.domain.task.models import InputBinding

    binding = InputBinding(
        binding_id="binding-1",
        task_id=task.task_id,
        input_type="plain_diff",
        object_identity="diff",
        base_sha=None,
        head_sha=None,
        content_digest="digest",
        completeness_digest="complete",
        changeset_ref="changeset",
    )
    bound = service.bind_input(task.task_id, binding)
    assert bound.input_binding == binding
    assert service.bind_input(task.task_id, binding) == bound
