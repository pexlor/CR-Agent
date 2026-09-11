"""Immutable report snapshot and presentation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from code_review_agent.domain.common.digests import sha256_digest


class ResultSnapshotKind(StrEnum):
    REVIEW = "review"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class CompletionView:
    state: str
    conclusion: str
    limitations: tuple[str, ...]
    next_step: str


@dataclass(frozen=True, slots=True)
class ReportModel:
    kind: str
    task_id: str
    snapshot_id: str
    snapshot_version: int
    checkpoint_id: str
    subject: str
    completion: CompletionView
    coverage: tuple[Any, ...]
    findings: tuple[Any, ...]
    budget: Any
    unknowns: tuple[Any, ...]
    trace: Any
    security: str
    report_model_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.kind not in {"review", "failure"}:
            raise ValueError("unsupported report kind")
        if not self.task_id or not self.snapshot_id or self.snapshot_version <= 0:
            raise ValueError("report identity is required")
        object.__setattr__(
            self,
            "report_model_digest",
            sha256_digest(
                {
                    "kind": self.kind,
                    "task_id": self.task_id,
                    "snapshot_id": self.snapshot_id,
                    "snapshot_version": self.snapshot_version,
                    "checkpoint_id": self.checkpoint_id,
                    "subject": self.subject,
                    "completion": {
                        "state": self.completion.state,
                        "conclusion": self.completion.conclusion,
                        "limitations": list(self.completion.limitations),
                        "next_step": self.completion.next_step,
                    },
                    "findings": [
                        getattr(item, "finding_id", str(item)) for item in self.findings
                    ],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ResultSnapshot:
    """A fixed result source; references are never filled from latest state."""

    snapshot_id: str
    task_id: str
    snapshot_version: int
    kind: ResultSnapshotKind
    result_state: str
    input_binding: Any
    review_plan: Any
    finding_set: Any
    budget_summary: Any
    unknown_attempts: tuple[Any, ...]
    trace_summary: Any
    checkpoint_id: str
    subject: str
    security_summary: str
    failure_stage: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.task_id or self.snapshot_version <= 0:
            raise ValueError("snapshot identity is required")
        if self.kind is ResultSnapshotKind.REVIEW:
            if any(
                item is None
                for item in (
                    self.input_binding,
                    self.review_plan,
                    self.finding_set,
                )
            ):
                raise ValueError("review snapshot references are required")
        elif any(item is not None for item in (self.finding_set, self.review_plan)):
            raise ValueError("failure snapshot cannot contain review references")
        for reference in (
            self.input_binding,
            self.review_plan,
            self.finding_set,
            self.budget_summary,
            self.trace_summary,
        ):
            if (
                reference is not None
                and getattr(reference, "task_id", self.task_id) != self.task_id
            ):
                raise ValueError("snapshot references must share task")
