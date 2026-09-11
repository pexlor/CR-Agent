"""Immutable Trace facts and mutation intents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.common.time import ensure_utc
from code_review_agent.domain.security.models import SanitizedArtifactRef


class FactKind(StrEnum):
    OBSERVATION = "observation"
    DECISION = "decision"
    TRANSITION = "transition"
    CORRECTION = "correction"


class TraceCategory(StrEnum):
    TASK = "task"
    INPUT = "input"
    SECURITY = "security"
    PLANNING = "planning"
    TOOL = "tool"
    MODEL = "model"
    BUDGET = "budget"
    FINDING = "finding"
    COMMENT = "comment"
    CHECKPOINT = "checkpoint"
    REPORT = "report"
    CORRECTION = "correction"
    CLEANUP = "cleanup"


class TraceRelation(StrEnum):
    CAUSED_BY = "caused_by"
    DERIVED_FROM = "derived_from"
    VALIDATED_BY = "validated_by"
    SETTLED_BY = "settled_by"
    CHECKPOINTED_BY = "checkpointed_by"
    SUPERSEDES = "supersedes"
    RELATES_TO = "relates_to"
    SHARES_CALL_WITH = "shares_call_with"
    INCLUDED_IN_REPORT = "included_in_report"


def _safe_summary(
    summary: Mapping[str, Any],
) -> MappingProxyType[str, str | int | float | bool | None]:
    normalized: dict[str, str | int | float | bool | None] = {}
    for key, value in summary.items():
        if not isinstance(key, str) or not key:
            raise TypeError("summary keys must be non-empty strings")
        if key.lower() in {
            "reasoning",
            "chain_of_thought",
            "content",
            "raw",
            "response",
        }:
            raise ValueError("trace summary contains a forbidden field")
        if type(value) not in (type(None), bool, int, float, str):
            raise TypeError("trace summary values must be scalar")
        normalized[key] = value
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class TraceEventDraft:
    """A fact proposal before sequence and event identity allocation."""

    event_type: str
    category: TraceCategory
    fact_kind: FactKind
    producer: str
    summary: Mapping[str, Any]
    event_version: int = 1
    correlation_id: str | None = None
    causation_event_id: str | None = None
    work_unit_id: str | None = None
    attempt_id: str | None = None
    model_call_id: str | None = None
    tool_call_id: str | None = None
    checkpoint_id: str | None = None
    payload_ref: SanitizedArtifactRef | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.event_type
            or " " in self.event_type
            or not all(
                part.isidentifier() and part.islower()
                for part in self.event_type.split(".")
            )
        ):
            raise ValueError("event_type must be a lowercase dot-separated name")
        if self.event_version <= 0 or not self.producer:
            raise ValueError("event version and producer must be positive/non-empty")
        object.__setattr__(self, "summary", _safe_summary(self.summary))


@dataclass(frozen=True, slots=True)
class TraceEventEnvelope:
    """A committed append-only fact with task-local sequence."""

    event_id: str
    task_id: str
    sequence: int
    event_type: str
    event_version: int
    category: TraceCategory
    fact_kind: FactKind
    occurred_at: datetime
    recorded_at: datetime
    producer: str
    correlation_id: str
    summary: Mapping[str, Any]
    idempotency_key: str
    retention_group_id: str
    schema_digest: str
    causation_event_id: str | None = None
    work_unit_id: str | None = None
    attempt_id: str | None = None
    model_call_id: str | None = None
    tool_call_id: str | None = None
    checkpoint_id: str | None = None
    payload_ref: SanitizedArtifactRef | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.task_id or self.sequence <= 0:
            raise ValueError("event identity and sequence must be positive/non-empty")
        if not self.idempotency_key or not self.retention_group_id:
            raise ValueError("event idempotency and retention keys are required")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "recorded_at", ensure_utc(self.recorded_at))
        object.__setattr__(self, "summary", _safe_summary(self.summary))


@dataclass(frozen=True, slots=True)
class TraceEdge:
    """Explicit same-task relation between two trace or business objects."""

    edge_id: str
    task_id: str
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    relation: TraceRelation | str
    created_event_id: str
    idempotency_key: str
    source_task_id: str
    target_task_id: str

    def __post_init__(self) -> None:
        if (
            not self.task_id
            or self.source_task_id != self.task_id
            or self.target_task_id != self.task_id
        ):
            raise ValueError("trace edge endpoints must belong to the same task")
        if not all(
            (
                self.edge_id,
                self.source_id,
                self.target_id,
                self.created_event_id,
                self.idempotency_key,
            )
        ):
            raise ValueError("trace edge identifiers are required")
        try:
            object.__setattr__(self, "relation", TraceRelation(self.relation))
        except ValueError as exc:
            raise ValueError("unknown trace relation") from exc


@dataclass(frozen=True, slots=True)
class TraceLink:
    """Current or historical trace evidence for one final finding."""

    trace_id: str
    task_id: str
    finding_id: str
    link_version: int
    direct_evidence_ids: tuple[str, ...]
    shared_process_ids: tuple[str, ...]
    validation_event_ids: tuple[str, ...]
    confidence_event_id: str | None
    budget_ledger_version: int
    checkpoint_id: str
    created_event_id: str
    supersedes_trace_id: str | None = None

    def __post_init__(self) -> None:
        if not self.trace_id or not self.task_id or not self.finding_id:
            raise ValueError("trace link identifiers are required")
        if self.link_version <= 0 or self.budget_ledger_version < 0:
            raise ValueError("trace link versions must be valid")
        if not self.direct_evidence_ids or any(
            not item for item in self.direct_evidence_ids
        ):
            raise ValueError("trace link requires direct evidence")
        if any(
            not item for item in (*self.shared_process_ids, *self.validation_event_ids)
        ):
            raise ValueError("trace link references must be non-empty")


@dataclass(frozen=True, slots=True)
class TraceMutationIntent:
    """Append-only mutation request consumed by a persistence UoW."""

    intent_id: str
    task_id: str
    business_transaction_kind: str
    events: tuple[TraceEventDraft, ...] = ()
    edges: tuple[TraceEdge, ...] = ()
    comment_link: TraceLink | None = None
    expected_task_version: int | None = None
    lease_id: str | None = None
    fencing_token: int | None = None
    idempotency_key: str = field(default_factory=lambda: str(uuid4()))
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.intent_id or not self.task_id or not self.business_transaction_kind:
            raise ValueError("trace intent identity is required")
        if self.expected_task_version is not None and self.expected_task_version < 1:
            raise ValueError("expected task version must be positive")
        if self.fencing_token is not None and self.fencing_token < 1:
            raise ValueError("fencing token must be positive")
        if any(edge.task_id != self.task_id for edge in self.edges):
            raise ValueError("trace intent contains a cross-task edge")
        object.__setattr__(
            self,
            "content_digest",
            sha256_digest(
                {
                    "task_id": self.task_id,
                    "transaction": self.business_transaction_kind,
                    "events": [
                        {
                            "type": event.event_type,
                            "version": event.event_version,
                            "category": event.category.value,
                            "fact_kind": event.fact_kind.value,
                            "producer": event.producer,
                            "summary": dict(event.summary),
                            "key": event.idempotency_key,
                        }
                        for event in self.events
                    ],
                    "edges": [edge.edge_id for edge in self.edges],
                    "comment_link": self.comment_link.trace_id
                    if self.comment_link
                    else None,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class TraceIntegrityResult:
    task_id: str
    valid: bool
    error_code: str | None = None
    checked_events: int = 0
    checked_links: int = 0


@dataclass(frozen=True, slots=True)
class TraceTimelineView:
    task_id: str
    events: tuple[TraceEventEnvelope, ...]
    edges: tuple[TraceEdge, ...]
    complete: bool = True
