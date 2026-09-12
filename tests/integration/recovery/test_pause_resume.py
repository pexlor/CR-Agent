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


class Steps:
    def __init__(self) -> None:
        self.executions = 0
        self.unknown_first = False

    def normalize(self, command: StartReviewCommand) -> object:
        return SimpleNamespace()

    def plan(self, normalized: object) -> object:
        return SimpleNamespace(work_units=(SimpleNamespace(execution_rank=1),))

    async def execute(self, unit: object) -> object:
        self.executions += 1
        return SimpleNamespace(state="unknown") if self.unknown_first else unit

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
        return SimpleNamespace(result_state="partial")

    def report(self, snapshot: object) -> object:
        return SimpleNamespace()

    def deliver(self, model: object, target: Path) -> object:
        return SimpleNamespace(path=target, content_digest="digest")


@pytest.mark.asyncio
async def test_pause_request_stops_external_steps_and_resume_clears_it(
    tmp_path: Path,
) -> None:
    steps = Steps()
    service = LocalDiffReviewService(ReviewOrchestrator())
    command = StartReviewCommand(
        task_id="task-recovery-1",
        diff_text="diff",
        diff_file=None,
        output_path=tmp_path / "review.md",
    )
    dependencies = ReviewDependencies(steps)

    service.request_pause(command.task_id, reason="user_requested")
    paused = await service.start(command, dependencies)

    assert steps.executions == 0
    assert paused.limitations == ("pause_requested",)

    resumed = await service.resume(command.task_id, dependencies)

    assert steps.executions == 1
    assert resumed.limitations == ()


def test_resume_unknown_requires_explicit_confirmation() -> None:
    service = LocalDiffReviewService(ReviewOrchestrator())
    service.mark_unknown("task-recovery-unknown")

    with pytest.raises(ValueError, match="unknown_retry_confirmation_required"):
        service.can_resume("task-recovery-unknown")

    assert service.can_resume(
        "task-recovery-unknown", confirm_unknown_retry=True
    )


@pytest.mark.asyncio
async def test_unknown_execution_stops_later_external_calls(tmp_path: Path) -> None:
    steps = Steps()
    steps.unknown_first = True
    service = LocalDiffReviewService(ReviewOrchestrator())
    command = StartReviewCommand(
        task_id="task-unknown-1",
        diff_text="diff",
        diff_file=None,
        output_path=tmp_path / "review.md",
    )

    result = await service.start(command, ReviewDependencies(steps))

    assert steps.executions == 1
    assert result.result_state == "unknown"
    assert result.limitations == ("unknown_execution",)
