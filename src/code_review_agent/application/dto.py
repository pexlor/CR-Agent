"""Immutable application input and query DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
class ReviewProgressView:
    task_id: str
    phase: str
    result_state: str
    delivery_state: str
    trace: tuple[TraceEventView, ...]


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
