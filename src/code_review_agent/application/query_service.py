"""Side-effect-free queries over application run results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from code_review_agent.application.dto import ReviewProgressView, ReviewRunResult
from code_review_agent.application.orchestration import ReviewOrchestrator
from code_review_agent.application.task_service import LocalDiffReviewService


@dataclass(frozen=True, slots=True)
class ReviewQueryService:
    orchestrator: ReviewOrchestrator
    runs: LocalDiffReviewService | None = None

    def get_progress(
        self,
        result: ReviewRunResult | str,
        trace: tuple[Any, ...] = (),
    ) -> ReviewProgressView:
        if isinstance(result, str):
            if self.runs is None:
                raise ValueError("run_store_required")
            result = self.runs.get(result)
        return self.orchestrator.progress(result, result.trace or tuple(trace))

    def get_trace(self, trace: tuple[Any, ...] | str) -> tuple[Any, ...]:
        if isinstance(trace, str):
            if self.runs is None:
                raise ValueError("run_store_required")
            return tuple(self.runs.get(trace).trace)
        return tuple(trace)
