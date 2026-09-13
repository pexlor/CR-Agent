from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from code_review_agent.application.dto import StartReviewCommand
from code_review_agent.application.orchestration import (
    ReviewDependencies,
    ReviewOrchestrator,
)
from code_review_agent.application.task_service import LocalDiffReviewService


class RecoverySteps:
    def __init__(self) -> None:
        self.calls = 0

    def normalize(self, command: StartReviewCommand) -> object:
        return command

    def plan(self, normalized: object) -> object:
        return SimpleNamespace(work_units=(SimpleNamespace(execution_rank=1),))

    async def execute(self, unit: object) -> object:
        self.calls += 1
        return unit

    def consolidate(
        self, normalized: object, plan: object, executions: tuple[object, ...]
    ) -> object:
        return SimpleNamespace()

    def snapshot(
        self,
        command: StartReviewCommand,
        normalized: object,
        plan: object,
        executions: tuple[object, ...],
        finding_set: object,
    ) -> object:
        return SimpleNamespace(result_state="complete_no_findings")

    def report(self, snapshot: object) -> object:
        return SimpleNamespace()

    def deliver(self, model: object, target: Path) -> object:
        return SimpleNamespace(path=target, content_digest="digest")


@pytest.mark.asyncio
async def test_pause_then_resume_reuses_task_context(tmp_path: Path) -> None:
    steps = RecoverySteps()
    service = LocalDiffReviewService(ReviewOrchestrator())
    command = StartReviewCommand("recovery", "diff", None, tmp_path / "review.md")

    service.request_pause(command.task_id, reason="injected interruption")
    paused = await service.start(command, ReviewDependencies(steps))
    resumed = await service.resume(command.task_id, ReviewDependencies(steps))

    assert paused.limitations == ("pause_requested",)
    assert resumed.result_state == "complete_no_findings"
    assert steps.calls == 1
