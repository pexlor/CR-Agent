"""In-memory application service for starting local review runs."""

from __future__ import annotations

from dataclasses import dataclass

from code_review_agent.application.dto import ReviewRunResult, StartReviewCommand
from code_review_agent.application.orchestration import (
    ReviewDependencies,
    ReviewOrchestrator,
)


@dataclass(slots=True)
class LocalDiffReviewService:
    orchestrator: ReviewOrchestrator
    _runs: dict[str, ReviewRunResult] | None = None

    def __post_init__(self) -> None:
        if self._runs is None:
            self._runs = {}

    async def start(
        self,
        command: StartReviewCommand,
        dependencies: ReviewDependencies,
    ) -> ReviewRunResult:
        result = await self.orchestrator.run(command, dependencies)
        assert self._runs is not None
        self._runs[result.task_id] = result
        return result

    def get(self, task_id: str) -> ReviewRunResult:
        assert self._runs is not None
        try:
            return self._runs[task_id]
        except KeyError as exc:
            raise ValueError("task_not_found") from exc


ReviewTaskService = LocalDiffReviewService
