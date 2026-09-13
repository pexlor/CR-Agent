"""Side-effect-free queries over application run results."""

from __future__ import annotations

from dataclasses import dataclass

from code_review_agent.application.dto import (
    PersistedTraceEventView,
    ReviewProgressView,
    ReviewRunResult,
    TraceEventView,
)
from code_review_agent.application.orchestration import ReviewOrchestrator
from code_review_agent.application.task_service import LocalDiffReviewService


@dataclass(frozen=True, slots=True)
class ReviewQueryService:
    orchestrator: ReviewOrchestrator
    runs: LocalDiffReviewService | None = None

    def get_progress(
        self,
        result: ReviewRunResult | str,
        trace: tuple[TraceEventView, ...] = (),
    ) -> ReviewProgressView:
        if isinstance(result, str):
            if self.runs is None:
                raise ValueError("run_store_required")
            result = self.runs.get(result)
        return self.orchestrator.progress(result, result.trace or tuple(trace))

    def get_trace(
        self, trace: tuple[TraceEventView | PersistedTraceEventView, ...] | str
    ) -> tuple[TraceEventView | PersistedTraceEventView, ...]:
        if isinstance(trace, str):
            if self.runs is None:
                raise ValueError("run_store_required")
            if self.runs.store is not None:
                try:
                    return self.runs.store.trace(trace)
                except ValueError as exc:
                    if str(exc) != "trace_not_found":
                        raise
                    if self.runs.store.has_trace_history(trace):
                        raise
            return tuple(self.runs.get(trace).trace)
        return tuple(trace)
