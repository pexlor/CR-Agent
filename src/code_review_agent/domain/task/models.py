"""Task aggregate, lifecycle records and immutable recovery objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.common.time import ensure_utc


class ControlState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    TERMINATED = "terminated"


class Phase(StrEnum):
    CREATED = "created"
    INPUT_ACQUIRED = "input_acquired"
    INPUT_NORMALIZED = "input_normalized"
    PLANNED = "planned"
    REVIEWING = "reviewing"
    CONSOLIDATING = "consolidating"
    RESULT_FINALIZED = "result_finalized"


class ResultState(StrEnum):
    PENDING = "pending"
    NO_CHANGES = "no_changes"
    COMPLETE_NO_FINDINGS = "complete_no_findings"
    COMPLETE_WITH_FINDINGS = "complete_with_findings"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DeliveryState(StrEnum):
    NOT_READY = "not_ready"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class LeasePurpose(StrEnum):
    REVIEW_EXECUTION = "review_execution"
    STOP_CONVERGENCE = "stop_convergence"
    REPORT_DELIVERY = "report_delivery"


class CheckpointKind(StrEnum):
    INPUT_ACQUIRED = "input_acquired"
    INPUT_NORMALIZED = "input_normalized"
    PLAN_CREATED = "plan_created"
    REVIEW_UNIT_COMPLETED = "review_unit_completed"
    TOOL_ATTEMPT_COMPLETED = "tool_attempt_completed"
    MODEL_ATTEMPT_COMPLETED = "model_attempt_completed"
    UNKNOWN_ATTEMPT_RECONCILED = "unknown_attempt_reconciled"
    FINDINGS_CONSOLIDATED = "findings_consolidated"
    RESULT_SNAPSHOT_CREATED = "result_snapshot_created"
    REPORT_GENERATED = "report_generated"
    REPORT_DELIVERY_COMPLETED = "report_delivery_completed"
    REPORT_DELIVERY_RECONCILED = "report_delivery_reconciled"


class AttemptState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    spec_id: str
    input_intent: str
    provider_id: str
    provider_version: str
    model_id: str
    provider_origin: str
    ruleset_id: str
    ruleset_version: str
    tools: tuple[str, ...]
    security_policy_id: str
    security_policy_version: int
    config_digest: str
    credential_alias: str
    budget_account_id: str
    fixed_conditions_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.spec_id,
                self.input_intent,
                self.provider_id,
                self.provider_version,
                self.model_id,
                self.provider_origin,
                self.ruleset_id,
                self.ruleset_version,
                self.security_policy_id,
                self.config_digest,
                self.credential_alias,
                self.budget_account_id,
            )
        ):
            raise ValueError("task spec fixed fields are required")
        if self.security_policy_version <= 0 or any(not tool for tool in self.tools):
            raise ValueError("task spec versions and tools must be valid")
        object.__setattr__(
            self,
            "fixed_conditions_digest",
            sha256_digest(
                {
                    "input_intent": self.input_intent,
                    "provider": [
                        self.provider_id,
                        self.provider_version,
                        self.model_id,
                        self.provider_origin,
                    ],
                    "rules": [self.ruleset_id, self.ruleset_version],
                    "tools": list(self.tools),
                    "security": [self.security_policy_id, self.security_policy_version],
                    "config_digest": self.config_digest,
                    "credential_alias": self.credential_alias,
                    "budget_account_id": self.budget_account_id,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class InputBinding:
    binding_id: str
    task_id: str
    input_type: str
    object_identity: str
    base_sha: str | None
    head_sha: str | None
    content_digest: str
    completeness_digest: str
    changeset_ref: str
    start_sha: str | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.binding_id,
                self.task_id,
                self.input_type,
                self.object_identity,
                self.content_digest,
                self.completeness_digest,
                self.changeset_ref,
            )
        ):
            raise ValueError("input binding fields are required")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    sequence: int
    kind: CheckpointKind
    phase: Phase
    scope_type: str
    scope_id: str
    predecessor_id: str | None
    budget_ledger_version: int
    trace_start_sequence: int
    trace_end_sequence: int
    created_task_version: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.checkpoint_id or not self.task_id or self.sequence <= 0:
            raise ValueError("checkpoint identity and sequence are required")
        if self.sequence > 1 and not self.predecessor_id:
            raise ValueError("non-initial checkpoint requires predecessor")
        if (
            self.budget_ledger_version < 0
            or self.trace_start_sequence < 0
            or self.trace_end_sequence < self.trace_start_sequence
        ):
            raise ValueError("checkpoint references must be valid")
        if self.created_task_version < 1:
            raise ValueError("checkpoint task version must be positive")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    lease_id: str
    owner_id: str
    fencing_token: int
    purpose: LeasePurpose
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    lease_revision: int = 1
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.lease_id or not self.owner_id or self.fencing_token < 1:
            raise ValueError("lease identity and fencing token are required")
        if self.lease_revision < 1:
            raise ValueError("lease revision must be positive")
        for field_name in ("acquired_at", "heartbeat_at", "expires_at"):
            object.__setattr__(self, field_name, ensure_utc(getattr(self, field_name)))
        if self.released_at is not None:
            object.__setattr__(self, "released_at", ensure_utc(self.released_at))
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease must expire after acquisition")

    def is_valid_at(self, now: datetime) -> bool:
        return self.released_at is None and ensure_utc(now) < self.expires_at


@dataclass(frozen=True, slots=True)
class ExecutionPermit:
    task_id: str
    task_version: int
    lease_id: str
    owner_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class ResultSnapshot:
    snapshot_id: str
    task_id: str
    snapshot_version: int
    result_state: ResultState
    checkpoint_id: str
    content_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.task_id or self.snapshot_version <= 0:
            raise ValueError("result snapshot identity and version are required")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    delivery_attempt_id: str
    task_id: str
    snapshot_id: str
    delivery_key: str
    expected_content_digest: str
    state: DeliveryState
    started_at: datetime
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    spec: TaskSpec
    control_state: ControlState = ControlState.READY
    phase: Phase = Phase.CREATED
    result_state: ResultState = ResultState.PENDING
    delivery_state: DeliveryState = DeliveryState.NOT_READY
    version: int = 1
    lease: ExecutionLease | None = None
    input_binding: InputBinding | None = None
    latest_checkpoint_id: str | None = None
    latest_result_snapshot_id: str | None = None
    latest_delivery_attempt_id: str | None = None
    pause_reason: str | None = None
    terminal_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_checkpoint_at: datetime | None = None
    recovery_expires_at: datetime | None = None
    retention_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.task_id or self.version < 1:
            raise ValueError("task identity and version are required")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))
        for field_name in (
            "last_checkpoint_at",
            "recovery_expires_at",
            "retention_expires_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value))


LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
RECOVERY_DAYS = 7


def recovery_deadline(now: datetime) -> datetime:
    return ensure_utc(now) + timedelta(days=RECOVERY_DAYS)
