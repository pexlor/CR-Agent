"""Immutable result contracts for deterministic finding processing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.execution.execution_models import CandidateLocation

if TYPE_CHECKING:
    from code_review_agent.domain.execution.execution_models import (
        WorkUnitExecutionResult,
    )

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Confidence(StrEnum):
    HIGH = "high"
    ADVISORY = "advisory"


class CoverageState(StrEnum):
    REVIEWED = "reviewed"
    UNREVIEWED = "unreviewed"
    UNREVIEWABLE = "unreviewable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FindingRejection:
    candidate_id: str
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FinalFinding:
    finding_id: str
    fingerprint: str
    task_id: str
    category: str
    severity: str
    confidence: Confidence
    title: str
    problem: str
    trigger_condition: str
    impact: str
    suggestion: str
    location: CandidateLocation
    evidence_ids: tuple[str, ...]
    trace_id: str
    source_candidate_ids: tuple[str, ...]
    confidence_basis: str
    limitations: str
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.finding_id,
                self.fingerprint,
                self.task_id,
                self.category,
                self.severity,
                self.title,
                self.problem,
                self.trigger_condition,
                self.impact,
                self.suggestion,
                self.trace_id,
                self.confidence_basis,
                self.schema_version,
            )
        ):
            raise ValueError("final finding identity and content are required")
        if not self.evidence_ids or not self.source_candidate_ids:
            raise ValueError("final finding must retain evidence and source candidates")
        if not _SHA256.fullmatch(self.fingerprint):
            raise ValueError("finding fingerprint must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    scope_id: str
    state: CoverageState
    reason_code: str


@dataclass(frozen=True, slots=True)
class CoverageSnapshot:
    task_id: str
    plan_id: str
    execution_fact_boundary_id: str
    entries: tuple[CoverageEntry, ...]
    snapshot_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.task_id or not self.plan_id or not self.execution_fact_boundary_id:
            raise ValueError("coverage snapshot identity is required")
        if not self.entries or len({entry.scope_id for entry in self.entries}) != len(
            self.entries
        ):
            raise ValueError("coverage entries must be unique and non-empty")
        object.__setattr__(
            self,
            "snapshot_digest",
            sha256_digest(
                {
                    "task_id": self.task_id,
                    "plan_id": self.plan_id,
                    "boundary": self.execution_fact_boundary_id,
                    "entries": [
                        [entry.scope_id, entry.state.value, entry.reason_code]
                        for entry in self.entries
                    ],
                }
            ),
        )

    def count(self, state: CoverageState) -> int:
        return sum(entry.state is state for entry in self.entries)


@dataclass(frozen=True, slots=True)
class FindingSet:
    task_id: str
    plan_id: str
    execution_fact_boundary_id: str
    findings: tuple[FinalFinding, ...]
    coverage: CoverageSnapshot
    rejections: tuple[FindingRejection, ...]
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.coverage.task_id != self.task_id
            or self.coverage.plan_id != self.plan_id
        ):
            raise ValueError("finding set and coverage must share identity")
        if self.coverage.execution_fact_boundary_id != self.execution_fact_boundary_id:
            raise ValueError("finding set and coverage must share fact boundary")
        fingerprints = [finding.fingerprint for finding in self.findings]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("finding fingerprints must be unique")
        object.__setattr__(
            self,
            "content_digest",
            sha256_digest(
                {
                    "task_id": self.task_id,
                    "plan_id": self.plan_id,
                    "boundary": self.execution_fact_boundary_id,
                    "coverage": self.coverage.snapshot_digest,
                    "findings": [
                        [item.finding_id, item.fingerprint, item.confidence.value]
                        for item in self.findings
                    ],
                    "rejections": [
                        [item.candidate_id, item.reason] for item in self.rejections
                    ],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class FindingProcessingRequest:
    request_id: str
    task_id: str
    execution_fact_boundary_id: str
    change_set: Any
    plan: Any
    executions: tuple[WorkUnitExecutionResult, ...]
    language_semantics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.request_id
            or not self.task_id
            or not self.execution_fact_boundary_id
        ):
            raise ValueError("finding processing request identity is required")
        if self.change_set.task_id != self.task_id or self.plan.task_id != self.task_id:
            raise ValueError("finding request references must share task")
        if self.plan.change_set_id != self.change_set.change_set_id:
            raise ValueError("finding request plan does not bind change set")
        if self.plan.change_set_digest != self.change_set.change_set_digest:
            raise ValueError("finding request digest mismatch")


@dataclass(frozen=True, slots=True)
class FindingProcessingResult:
    finding_set: FindingSet
    rejections: tuple[FindingRejection, ...]
