from __future__ import annotations

from pathlib import Path

from code_review_agent.application.dto import ReviewRunResult, TraceEventView
from code_review_agent.application.persistence import ReviewStateStore


def test_review_state_store_survives_a_new_instance(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    result = ReviewRunResult(
        task_id="task-1",
        session_id="session-1",
        phase="completed",
        result_state="unknown",
        delivery_state="succeeded",
        report_path=Path("reports/task-1.md"),
        report_digest="digest",
        limitations=("unknown_execution",),
        trace=(TraceEventView(1, "executing", "phase advanced"),),
    )

    ReviewStateStore(database).save(result)
    loaded = ReviewStateStore(database).get("task-1")

    assert loaded == result


def test_review_state_store_records_pause_and_resume_requests(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")

    store.request_pause("task-1", "user_requested")
    assert store.is_paused("task-1")

    store.clear_pause("task-1")
    assert not store.is_paused("task-1")
