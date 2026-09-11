"""Task aggregate lifecycle, lease and checkpoint operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

from code_review_agent.domain.common.time import Clock, ensure_utc
from code_review_agent.domain.task.models import (
    LEASE_SECONDS,
    Checkpoint,
    ControlState,
    DeliveryState,
    ExecutionLease,
    ExecutionPermit,
    InputBinding,
    LeasePurpose,
    Phase,
    ResultSnapshot,
    ResultState,
    Task,
    TaskSpec,
    recovery_deadline,
)
from code_review_agent.domain.task.state_machine import (
    advance_phase,
    validate_control_transition,
)


class TaskService:
    """In-memory task aggregate used before persistence adapters are available."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._tasks: dict[str, Task] = {}
        self._checkpoints: dict[str, list[Checkpoint]] = {}
        self._snapshots: dict[str, list[ResultSnapshot]] = {}
        self._unknown_attempts: set[str] = set()
        self._fencing_tokens: dict[str, int] = {}

    def create_task(self, spec: TaskSpec) -> Task:
        task_id = str(uuid4())
        now = self._clock.now()
        task = Task(
            task_id=task_id,
            spec=spec,
            created_at=now,
            updated_at=now,
            recovery_expires_at=recovery_deadline(now),
        )
        self._tasks[task_id] = task
        self._checkpoints[task_id] = []
        self._snapshots[task_id] = []
        self._fencing_tokens[task_id] = 0
        return task

    def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise ValueError("task_not_found") from exc

    def acquire_execution(self, task_id: str, *, owner_id: str) -> ExecutionPermit:
        task = self.get(task_id)
        now = self._clock.now()
        if task.control_state is not ControlState.READY:
            if (
                task.control_state is ControlState.RUNNING
                and task.lease is not None
                and not task.lease.is_valid_at(now)
            ):
                raise ValueError("resume_required")
            raise ValueError("task_already_running")
        if task.lease is not None and task.lease.is_valid_at(now):
            raise ValueError("task_already_running")
        lease = self._new_lease(task, owner_id, LeasePurpose.REVIEW_EXECUTION, now)
        updated = replace(
            task,
            control_state=ControlState.RUNNING,
            lease=lease,
            version=task.version + 1,
            updated_at=now,
        )
        self._tasks[task_id] = updated
        return ExecutionPermit(
            task_id, updated.version, lease.lease_id, owner_id, lease.fencing_token
        )

    def renew_lease(
        self, task_id: str, lease_id: str, fencing_token: int
    ) -> ExecutionLease:
        task = self.get(task_id)
        lease = self._require_lease(task, lease_id, fencing_token)
        now = self._clock.now()
        if not lease.is_valid_at(now):
            raise ValueError("lease_lost")
        renewed = replace(
            lease,
            heartbeat_at=now,
            expires_at=ensure_utc(now) + timedelta(seconds=LEASE_SECONDS),
            lease_revision=lease.lease_revision + 1,
        )
        self._tasks[task_id] = replace(task, lease=renewed)
        return renewed

    def release_execution(
        self, task_id: str, lease_id: str, fencing_token: int
    ) -> None:
        task = self.get(task_id)
        lease = self._require_lease(task, lease_id, fencing_token)
        now = self._clock.now()
        self._tasks[task_id] = replace(
            task, lease=replace(lease, released_at=now), updated_at=now
        )

    def complete_phase(
        self,
        task_id: str,
        *,
        expected_version: int,
        lease_id: str,
        fencing_token: int,
        target_phase: Phase,
        checkpoint: Checkpoint,
    ) -> Task:
        task = self.get(task_id)
        self._require_write(task, expected_version, lease_id, fencing_token)
        new_phase = advance_phase(task.phase, target_phase)
        expected_sequence = len(self._checkpoints[task_id]) + 1
        if checkpoint.task_id != task_id or checkpoint.sequence != expected_sequence:
            raise ValueError("checkpoint_integrity_error")
        if (
            checkpoint.sequence > 1
            and checkpoint.predecessor_id
            != self._checkpoints[task_id][-1].checkpoint_id
        ):
            raise ValueError("checkpoint_integrity_error")
        self._checkpoints[task_id].append(checkpoint)
        now = self._clock.now()
        updated = replace(
            task,
            phase=new_phase,
            latest_checkpoint_id=checkpoint.checkpoint_id,
            last_checkpoint_at=now,
            recovery_expires_at=recovery_deadline(now),
            version=task.version + 1,
            updated_at=now,
        )
        self._tasks[task_id] = updated
        return updated

    def bind_input(self, task_id: str, binding: InputBinding) -> Task:
        task = self.get(task_id)
        if binding.task_id != task_id:
            raise ValueError("fixed_condition_mismatch")
        if task.input_binding is not None:
            if task.input_binding == binding:
                return task
            raise ValueError("fixed_condition_mismatch")
        if task.phase is not Phase.INPUT_NORMALIZED:
            raise ValueError("illegal_state_transition")
        now = self._clock.now()
        updated = replace(
            task, input_binding=binding, version=task.version + 1, updated_at=now
        )
        self._tasks[task_id] = updated
        return updated

    def pause(
        self,
        task_id: str,
        *,
        expected_version: int,
        lease_id: str,
        fencing_token: int,
        reason: str,
        result_state: ResultState = ResultState.PENDING,
    ) -> Task:
        task = self.get(task_id)
        self._require_write(task, expected_version, lease_id, fencing_token)
        validate_control_transition(task.control_state, ControlState.PAUSED)
        now = self._clock.now()
        if result_state is ResultState.UNKNOWN:
            self._unknown_attempts.add(task_id)
        updated = replace(
            task,
            control_state=ControlState.PAUSED,
            result_state=result_state,
            pause_reason=reason,
            lease=None,
            version=task.version + 1,
            updated_at=now,
        )
        self._tasks[task_id] = updated
        return updated

    def resume(
        self,
        task_id: str,
        *,
        owner_id: str,
        confirm_unknown_retry: bool = False,
    ) -> ExecutionPermit:
        task = self.get(task_id)
        now = self._clock.now()
        if task.control_state is ControlState.TERMINATED:
            raise ValueError("illegal_state_transition")
        if (
            task.control_state is ControlState.RUNNING
            and task.lease is not None
            and task.lease.is_valid_at(now)
        ):
            raise ValueError("task_already_running")
        if task.control_state not in (ControlState.PAUSED, ControlState.RUNNING):
            raise ValueError("resume_condition_blocked")
        if task_id in self._unknown_attempts and not confirm_unknown_retry:
            raise ValueError("unknown_retry_confirmation_required")
        lease = self._new_lease(task, owner_id, LeasePurpose.REVIEW_EXECUTION, now)
        updated = replace(
            task,
            control_state=ControlState.RUNNING,
            lease=lease,
            version=task.version + 1,
            updated_at=now,
        )
        self._tasks[task_id] = updated
        self._unknown_attempts.discard(task_id)
        return ExecutionPermit(
            task_id, updated.version, lease.lease_id, owner_id, lease.fencing_token
        )

    def terminate(
        self,
        task_id: str,
        *,
        expected_version: int,
        reason: str,
        lease_id: str | None = None,
        fencing_token: int | None = None,
    ) -> Task:
        task = self.get(task_id)
        if task.control_state is ControlState.TERMINATED:
            return task
        if task.control_state is ControlState.RUNNING:
            if lease_id is None or fencing_token is None:
                raise ValueError("lease_lost")
            self._require_write(task, expected_version, lease_id, fencing_token)
        elif task.version != expected_version:
            raise ValueError("version_conflict")
        now = self._clock.now()
        updated = replace(
            task,
            control_state=ControlState.TERMINATED,
            terminal_reason=reason,
            lease=None,
            version=task.version + 1,
            updated_at=now,
            retention_expires_at=ensure_utc(now) + timedelta(days=7),
        )
        self._tasks[task_id] = updated
        return updated

    def create_result_snapshot(
        self,
        task_id: str,
        *,
        result_state: ResultState,
        checkpoint_id: str,
        content_digest: str,
    ) -> ResultSnapshot:
        task = self.get(task_id)
        if checkpoint_id not in {
            checkpoint.checkpoint_id for checkpoint in self._checkpoints[task_id]
        }:
            raise ValueError("checkpoint_integrity_error")
        version = len(self._snapshots[task_id]) + 1
        snapshot = ResultSnapshot(
            snapshot_id=str(uuid4()),
            task_id=task_id,
            snapshot_version=version,
            result_state=result_state,
            checkpoint_id=checkpoint_id,
            content_digest=content_digest,
            created_at=self._clock.now(),
        )
        self._snapshots[task_id].append(snapshot)
        self._tasks[task_id] = replace(
            task,
            latest_result_snapshot_id=snapshot.snapshot_id,
            result_state=result_state,
            delivery_state=DeliveryState.PENDING,
            version=task.version + 1,
            updated_at=self._clock.now(),
        )
        return snapshot

    def recoverability(self, task_id: str) -> str:
        task = self.get(task_id)
        now = self._clock.now()
        if task.control_state is ControlState.TERMINATED:
            if task.delivery_state in (
                DeliveryState.PENDING,
                DeliveryState.FAILED,
                DeliveryState.UNKNOWN,
            ):
                return "retry_pending"
            return "not_applicable"
        if (
            task.recovery_expires_at is not None
            and ensure_utc(now) > task.recovery_expires_at
        ):
            return "expired"
        if (
            task.control_state is ControlState.RUNNING
            and task.lease is not None
            and task.lease.is_valid_at(now)
        ):
            return "running"
        return "recoverable"

    def _new_lease(
        self, task: Task, owner_id: str, purpose: LeasePurpose, now: datetime
    ) -> ExecutionLease:
        token = self._fencing_tokens[task.task_id] + 1
        self._fencing_tokens[task.task_id] = token
        return ExecutionLease(
            lease_id=str(uuid4()),
            owner_id=owner_id,
            fencing_token=token,
            purpose=purpose,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=ensure_utc(now) + timedelta(seconds=LEASE_SECONDS),
        )

    @staticmethod
    def _require_lease(task: Task, lease_id: str, fencing_token: int) -> ExecutionLease:
        lease = task.lease
        if (
            lease is None
            or lease.lease_id != lease_id
            or lease.fencing_token != fencing_token
        ):
            raise ValueError("lease_lost")
        return lease

    def _require_write(
        self, task: Task, expected_version: int, lease_id: str, fencing_token: int
    ) -> None:
        if task.version != expected_version:
            raise ValueError("version_conflict")
        lease = self._require_lease(task, lease_id, fencing_token)
        if not lease.is_valid_at(self._clock.now()):
            raise ValueError("lease_lost")
