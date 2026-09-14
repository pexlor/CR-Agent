"""Immutable application input and query DTOs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from code_review_agent.domain.publication.models import PublicationResult

type TraceSummaryScalar = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class StartReviewCommand:
    task_id: str
    diff_text: str | None
    diff_file: Path | None
    output_path: Path
    subject: str = "Local diff review"
    source_url: str | None = None
    provider: str = ""
    model: str = ""
    budget_tokens: int = 0
    publish: bool = False

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        sources = sum(
            value is not None
            for value in (self.diff_text, self.diff_file, self.source_url)
        )
        if sources != 1:
            raise ValueError("exactly one diff source is required")


@dataclass(frozen=True, slots=True)
class TraceEventView:
    sequence: int
    phase: str
    message: str


@dataclass(frozen=True, slots=True)
class PersistedTraceArtifactView:
    artifact_id: str
    purpose: str
    content: str
    content_digest: str
    security_decision: str
    created_at: str = ""
    expires_at: str = ""


@dataclass(frozen=True, slots=True)
class PersistedTraceEventView:
    event_id: str
    task_id: str
    sequence: int
    event_type: str
    category: str
    summary: Mapping[str, TraceSummaryScalar]
    idempotency_key: str
    created_at: str
    expires_at: str
    artifact: PersistedTraceArtifactView | None = None


@dataclass(frozen=True, slots=True)
class TraceReportView:
    task_id: str
    event_count: int
    model_call_count: int
    accepted_count: int
    rejected_count: int
    error_count: int
    query_command: str


@dataclass(frozen=True, slots=True)
class BudgetReportView:
    task_id: str
    authorized: int
    known_consumption: int
    uncertain_consumption: int
    active_reservations: int
    remaining_budget: int
    overage: int
    authorization_deficit: int
    account_state: str
    ledger_version: int
    currency: str
    configured_cost_limit: str
    price_per_million_tokens: str
    authorized_cost: str
    known_cost: str
    uncertain_cost: str
    active_reservations_cost: str
    remaining_cost: str
    overage_cost: str
    authorization_deficit_cost: str


@dataclass(frozen=True, slots=True)
class ReviewProgressView:
    task_id: str
    phase: str
    result_state: str
    delivery_state: str
    trace: tuple[TraceEventView, ...]
    publication_state: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewRunResult:
    task_id: str
    session_id: str
    phase: str
    result_state: str
    delivery_state: str
    report_path: Path | None
    report_digest: str | None
    limitations: tuple[str, ...] = ()
    trace: tuple[TraceEventView, ...] = ()
    publication: PublicationResult | None = None
