from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_review_agent.application.dto import (
    PersistedTraceEventView,
    ReviewRunResult,
    StartReviewCommand,
    TraceEventView,
)
from code_review_agent.application.orchestration import (
    ExecutionSession,
    ReviewDependencies,
    ReviewOrchestrator,
    SessionPhase,
)
from code_review_agent.application.persistence import ReviewStateStore
from code_review_agent.application.query_service import ReviewQueryService
from code_review_agent.application.task_service import LocalDiffReviewService


@dataclass(frozen=True)
class _Unit:
    execution_rank: int


class _Steps:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.executed: list[int] = []

    def normalize(self, command: StartReviewCommand) -> object:
        self.calls.append("normalize")
        return SimpleNamespace()

    def plan(self, normalized: object) -> object:
        self.calls.append("plan")
        return SimpleNamespace(work_units=(_Unit(2), _Unit(1)))

    async def execute(self, unit: _Unit) -> object:
        self.calls.append(f"execute:{unit.execution_rank}")
        self.executed.append(unit.execution_rank)
        return unit

    def consolidate(
        self, normalized: object, plan: object, executions: tuple[object, ...]
    ) -> object:
        self.calls.append("consolidate")
        return SimpleNamespace()

    def snapshot(
        self,
        command: StartReviewCommand,
        normalized: object,
        plan: object,
        executions: tuple[object, ...],
        finding_set: object,
    ) -> object:
        self.calls.append("snapshot")
        return SimpleNamespace(result_state="complete_no_findings")

    def report(self, snapshot: object) -> object:
        self.calls.append("report")
        return SimpleNamespace()

    def deliver(self, model: object, target: Path) -> object:
        self.calls.append("deliver")
        return SimpleNamespace(path=target, content_digest="digest")


def test_execution_session_only_advances_monotonically() -> None:
    session = ExecutionSession("task-1")

    session.advance(SessionPhase.INPUT_NORMALIZING)
    session.advance(SessionPhase.PLANNING)

    with pytest.raises(ValueError, match="illegal_phase_transition"):
        session.advance(SessionPhase.INPUT_NORMALIZING)


@pytest.mark.asyncio
async def test_orchestrator_runs_frozen_units_in_order(tmp_path: Path) -> None:
    steps = _Steps()
    command = StartReviewCommand(
        task_id="task-1",
        diff_text="@@ -1 +1 @@\n-old\n+new",
        diff_file=None,
        output_path=tmp_path / "review.md",
    )

    result = await ReviewOrchestrator().run(command, ReviewDependencies(steps))

    assert steps.executed == [1, 2]
    assert result.result_state == "complete_no_findings"
    assert result.report_path == command.output_path
    assert steps.calls == [
        "normalize",
        "plan",
        "execute:1",
        "execute:2",
        "consolidate",
        "snapshot",
        "report",
        "deliver",
    ]


@pytest.mark.asyncio
async def test_stop_check_prevents_later_external_calls(tmp_path: Path) -> None:
    steps = _Steps()
    command = StartReviewCommand(
        task_id="task-1",
        diff_text="@@ -1 +1 @@\n-old\n+new",
        diff_file=None,
        output_path=tmp_path / "review.md",
    )

    result = await ReviewOrchestrator().run(
        command,
        ReviewDependencies(steps, should_stop=lambda: True),
    )

    assert steps.executed == []
    assert result.limitations == ("stop_requested",)


def test_query_service_returns_immutable_trace_view(tmp_path: Path) -> None:
    result = ReviewRunResult(
        task_id="task-1",
        session_id="session-1",
        phase="completed",
        result_state="complete_no_findings",
        delivery_state="succeeded",
        report_path=tmp_path / "review.md",
        report_digest="digest",
    )
    trace = (TraceEventView(1, "completed", "done"),)

    view = ReviewQueryService(ReviewOrchestrator()).get_progress(result, trace)

    assert view.trace == trace
    assert ReviewQueryService(ReviewOrchestrator()).get_trace(trace) == trace


@pytest.mark.asyncio
async def test_task_service_persists_result_for_read_only_queries(
    tmp_path: Path,
) -> None:
    steps = _Steps()
    service = LocalDiffReviewService(ReviewOrchestrator())
    command = StartReviewCommand(
        task_id="task-1",
        diff_text="@@ -1 +1 @@\n-old\n+new",
        diff_file=None,
        output_path=tmp_path / "review.md",
    )

    result = await service.start(command, ReviewDependencies(steps))
    view = ReviewQueryService(ReviewOrchestrator(), service).get_progress("task-1")

    assert view.task_id == result.task_id
    assert view.trace == result.trace


def test_query_service_prefers_persisted_trace_and_falls_back_to_stage_trace(
    tmp_path: Path,
) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")
    legacy = ReviewRunResult(
        task_id="task-1",
        session_id="session-1",
        phase="completed",
        result_state="complete_no_findings",
        delivery_state="succeeded",
        report_path=None,
        report_digest=None,
        trace=(TraceEventView(1, "completed", "done"),),
    )
    store.save(legacy)
    service = LocalDiffReviewService(ReviewOrchestrator(), store=store)
    queries = ReviewQueryService(ReviewOrchestrator(), service)
    assert queries.get_trace("task-1") == legacy.trace

    detailed = store.append_trace_event(
        task_id="task-1",
        event_id="event-1",
        event_type="report.delivered",
        category="report",
        summary={"delivered": True},
        idempotency_key="report-delivered",
    )
    result = queries.get_trace("task-1")
    assert result == (detailed,)
    assert isinstance(result[0], PersistedTraceEventView)


@pytest.mark.parametrize("remove_mode", ["cleanup", "expiry"])
def test_query_service_does_not_fallback_after_detailed_trace_removal(
    tmp_path: Path, remove_mode: str
) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    legacy = ReviewRunResult(
        task_id="task-1",
        session_id="session-1",
        phase="completed",
        result_state="complete_no_findings",
        delivery_state="succeeded",
        report_path=None,
        report_digest=None,
        trace=(TraceEventView(1, "completed", "legacy fallback"),),
    )
    store.save(legacy)
    store.append_trace_event(
        task_id="task-1",
        event_id="event-1",
        event_type="report.delivered",
        category="report",
        summary={"delivered": True},
        idempotency_key="report-delivered",
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=1)
            if remove_mode == "expiry"
            else None
        ),
    )
    if remove_mode == "cleanup":
        store.cleanup_trace("task-1")
    else:
        store = ReviewStateStore(database)
    queries = ReviewQueryService(
        ReviewOrchestrator(), LocalDiffReviewService(ReviewOrchestrator(), store=store)
    )

    with pytest.raises(ValueError, match="trace_not_found"):
        queries.get_trace("task-1")
