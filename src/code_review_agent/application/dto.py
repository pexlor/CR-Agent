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

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        if (self.diff_text is None) == (self.diff_file is None):
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
