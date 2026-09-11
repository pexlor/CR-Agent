"""Execution-time value objects: attempts, candidates and outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.execution.models import ModelCallOutcome
from code_review_agent.domain.planning.models import ToolFailureImpact
from code_review_agent.domain.security.models import SanitizedArtifactRef
from code_review_agent.ports.tools import ToolExecutionResult

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WorkUnitExecutionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ToolAttemptState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_KNOWN = "failed_known"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"


class ModelAttemptOutcomeKind(StrEnum):
    """How this WorkUnitExecution's single model attempt concluded."""

    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    FAILED_KNOWN = "failed_known"
    UNKNOWN = "unknown"


class CoverageImpact(StrEnum):
    """Effect of this execution on the planned scope's final coverage."""

    FULLY_COVERED = "fully_covered"
    DEGRADED = "degraded"
    NOT_COVERED = "not_covered"


class CandidateLocationKind(StrEnum):
    NEW_LINE = "new_line"
    OLD_LINE = "old_line"
    FILE = "file"
    MULTI_FILE = "multi_file"


@dataclass(frozen=True, slots=True)
class CandidateLocation:
    kind: CandidateLocationKind
    file_id: str
    line: int | None = None
    related_file_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.file_id:
            raise ValueError("candidate location requires a file id")
        if self.kind in (
            CandidateLocationKind.NEW_LINE,
            CandidateLocationKind.OLD_LINE,
        ):
            if self.line is None or self.line <= 0:
                raise ValueError("line-based locations require a positive line number")
        elif self.line is not None:
            raise ValueError("file/multi-file locations cannot carry a line number")
        if self.kind is CandidateLocationKind.MULTI_FILE and not self.related_file_ids:
            raise ValueError("multi-file locations require related file ids")
        if self.kind is not CandidateLocationKind.MULTI_FILE and self.related_file_ids:
            raise ValueError("only multi-file locations carry related file ids")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A stable reference to material actually produced by this execution."""

    evidence_id: str
    evidence_type: str
    source_ref: str
    location: CandidateLocation | None
    content_digest: str

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.evidence_type or not self.source_ref:
            raise ValueError("evidence reference identity is required")
        if not _SHA256.fullmatch(self.content_digest):
            raise ValueError("evidence content digest must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class CandidateFinding:
    """A structurally validated model output, not yet a final finding."""

    candidate_id: str
    task_id: str
    work_unit_id: str
    execution_id: str
    category: str
    title: str
    problem: str
    trigger_condition: str
    location: CandidateLocation
    evidence_refs: tuple[str, ...]
    impact: str
    suggestion: str
    change_causation: str
    limitations: str
    schema_version: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.candidate_id,
                self.task_id,
                self.work_unit_id,
                self.execution_id,
                self.category,
                self.title,
                self.problem,
                self.trigger_condition,
                self.impact,
                self.suggestion,
                self.change_causation,
                self.schema_version,
            )
        ):
            raise ValueError("candidate finding fixed fields are required")
        if not self.evidence_refs or any(
            not isinstance(item, str) or not item for item in self.evidence_refs
        ):
            raise ValueError("candidate finding requires non-empty evidence refs")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("candidate finding evidence refs must be unique")


@dataclass(frozen=True, slots=True)
class ToolAttempt:
    tool_attempt_id: str
    work_unit_id: str
    execution_id: str
    tool_id: str
    tool_version: str
    rule_summary_digest: str
    input_digest: str
    state: ToolAttemptState
    failure_impact: ToolFailureImpact
    result: ToolExecutionResult | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.tool_attempt_id,
                self.work_unit_id,
                self.execution_id,
                self.tool_id,
                self.tool_version,
                self.rule_summary_digest,
                self.input_digest,
            )
        ):
            raise ValueError("tool attempt identity is required")
        if self.state is ToolAttemptState.SUCCEEDED and self.result is None:
            raise ValueError("a succeeded tool attempt must carry its result")
        if (
            self.state in (ToolAttemptState.FAILED_KNOWN, ToolAttemptState.BLOCKED)
            and self.error_code is None
        ):
            raise ValueError("a failed or blocked tool attempt requires an error code")


@dataclass(frozen=True, slots=True)
class ModelCallAttempt:
    model_call_id: str
    work_unit_id: str
    execution_id: str
    provider_id: str
    model_id: str
    request_ref: SanitizedArtifactRef | None
    response_ref: SanitizedArtifactRef | None
    reservation_id: str
    outcome: ModelCallOutcome | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.model_call_id,
                self.work_unit_id,
                self.execution_id,
                self.provider_id,
                self.model_id,
                self.reservation_id,
            )
        ):
            raise ValueError("model call attempt identity is required")


@dataclass(frozen=True, slots=True)
class WorkUnitExecutionResult:
    """The full, immutable outcome of executing one fixed WorkUnit."""

    execution_id: str
    task_id: str
    work_unit_id: str
    plan_id: str
    attempt_number: int
    state: WorkUnitExecutionState
    coverage_impact: CoverageImpact
    tool_attempts: tuple[ToolAttempt, ...]
    model_attempt: ModelCallAttempt | None
    model_outcome_kind: ModelAttemptOutcomeKind
    candidates: tuple[CandidateFinding, ...]
    evidence: tuple[EvidenceReference, ...] = ()
    error_code: str | None = None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.execution_id,
                self.task_id,
                self.work_unit_id,
                self.plan_id,
            )
        ):
            raise ValueError("work unit execution identity is required")
        if self.attempt_number <= 0:
            raise ValueError("attempt number must be positive")
        evidence_ids = {item.evidence_id for item in self.evidence}
        for candidate in self.candidates:
            if not set(candidate.evidence_refs).issubset(evidence_ids):
                raise ValueError(
                    "candidate evidence refs must resolve to this execution's "
                    "own evidence"
                )
        if self.model_attempt is not None and self.model_outcome_kind not in (
            ModelAttemptOutcomeKind.SUCCEEDED,
            ModelAttemptOutcomeKind.FAILED_KNOWN,
            ModelAttemptOutcomeKind.UNKNOWN,
        ):
            raise ValueError("a bound model attempt must have a terminal outcome")
        if self.model_attempt is None and self.model_outcome_kind is not (
            ModelAttemptOutcomeKind.NOT_ATTEMPTED
        ):
            raise ValueError("no model attempt requires the not_attempted outcome")
        if self.candidates and self.model_outcome_kind is not (
            ModelAttemptOutcomeKind.SUCCEEDED
        ):
            raise ValueError("candidates require a succeeded model attempt")
        for candidate in self.candidates:
            if (
                candidate.execution_id != self.execution_id
                or candidate.work_unit_id != self.work_unit_id
                or candidate.task_id != self.task_id
            ):
                raise ValueError(
                    "candidates may only reference this execution's own material"
                )
        if self.state is WorkUnitExecutionState.UNKNOWN and (
            self.model_outcome_kind is not ModelAttemptOutcomeKind.UNKNOWN
        ):
            raise ValueError(
                "unknown execution state requires an unknown model outcome"
            )
        object.__setattr__(
            self,
            "fingerprint",
            sha256_digest(
                {
                    "task_id": self.task_id,
                    "work_unit_id": self.work_unit_id,
                    "plan_id": self.plan_id,
                    "attempt_number": self.attempt_number,
                    "state": self.state.value,
                    "coverage_impact": self.coverage_impact.value,
                    "model_outcome_kind": self.model_outcome_kind.value,
                    "candidate_ids": sorted(
                        candidate.candidate_id for candidate in self.candidates
                    ),
                }
            ),
        )
