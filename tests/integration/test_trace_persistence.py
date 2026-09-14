from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from code_review_agent.application.dto import PersistedTraceArtifactView
from code_review_agent.application.persistence import ReviewStateStore


def _artifact(content: str = "redacted request") -> PersistedTraceArtifactView:
    return PersistedTraceArtifactView(
        artifact_id="artifact-1",
        purpose="trace_model_request",
        content=content,
        content_digest=hashlib.sha256(content.encode()).hexdigest(),
        security_decision="redacted",
    )


def test_appends_event_and_artifact_atomically_with_continuous_sequence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    first = store.append_trace_event(
        task_id="task-1",
        event_id="event-1",
        event_type="model.call_started",
        category="model",
        summary={"attempt": 1, "cached": False},
        idempotency_key="call-1-started",
        artifact=_artifact(),
    )
    second = store.append_trace_event(
        task_id="task-1",
        event_id="event-2",
        event_type="budget.settled",
        category="budget",
        summary={"known_tokens": 12},
        idempotency_key="call-1-budget",
    )
    assert (first.sequence, second.sequence) == (1, 2)
    assert ReviewStateStore(database).trace("task-1") == (first, second)
    assert first.artifact is not None
    assert first.artifact.content == _artifact().content
    assert first.artifact.content_digest == _artifact().content_digest
    assert first.artifact.created_at
    assert first.artifact.expires_at


def test_idempotency_replays_same_event_but_rejects_conflicting_payload(
    tmp_path: Path,
) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")
    original = store.append_trace_event(
        task_id="task-1",
        event_id="event-1",
        event_type="model.call_succeeded",
        category="model",
        summary={"attempt": 1},
        idempotency_key="call-1-succeeded",
    )
    replay = store.append_trace_event(
        task_id="task-1",
        event_id="event-1",
        event_type="model.call_succeeded",
        category="model",
        summary={"attempt": 1},
        idempotency_key="call-1-succeeded",
    )
    assert replay == original
    with pytest.raises(ValueError, match="trace_idempotency_conflict"):
        store.append_trace_event(
            task_id="task-1",
            event_id="event-1",
            event_type="model.call_succeeded",
            category="model",
            summary={"attempt": 2},
            idempotency_key="call-1-succeeded",
        )
    assert len(store.trace("task-1")) == 1


@pytest.mark.parametrize(
    "summary",
    [
        {"nested": {"value": 1}},
        {"items": [1]},
        {"reasoning": "hidden"},
        {"chain_of_thought": "hidden"},
        {"content": "secret"},
        {"raw": "secret"},
        {"response": "secret"},
    ],
)
def test_rejects_non_scalar_or_forbidden_summary(
    tmp_path: Path, summary: dict[str, object]
) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="trace_summary_invalid"):
        store.append_trace_event(
            task_id="task-1",
            event_id="event-1",
            event_type="model.call_started",
            category="model",
            summary=summary,
            idempotency_key="key-1",
        )


def test_digest_failure_rolls_back_event_and_artifact(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    artifact = _artifact()
    invalid = PersistedTraceArtifactView(
        artifact_id=artifact.artifact_id,
        purpose=artifact.purpose,
        content=artifact.content,
        content_digest="0" * 64,
        security_decision=artifact.security_decision,
    )
    with pytest.raises(ValueError, match="trace_artifact_digest_mismatch"):
        store.append_trace_event(
            task_id="task-1",
            event_id="event-1",
            event_type="model.call_started",
            category="model",
            summary={},
            idempotency_key="key-1",
            artifact=invalid,
        )
    with sqlite3.connect(database) as connection:
        event_count = connection.execute(
            "SELECT count(*) FROM review_trace_events"
        ).fetchone()
        artifact_count = connection.execute(
            "SELECT count(*) FROM review_trace_artifacts"
        ).fetchone()
        assert event_count == (0,)
        assert artifact_count == (0,)


def test_initialization_removes_expired_detail_and_cleanup_removes_task_detail(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    store.append_trace_event(
        task_id="expired-task",
        event_id="expired-event",
        event_type="input.normalized",
        category="input",
        summary={},
        idempotency_key="expired-key",
        artifact=_artifact(),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    restarted = ReviewStateStore(database)
    with pytest.raises(ValueError, match="trace_not_found"):
        restarted.trace("expired-task")
    restarted.append_trace_event(
        task_id="live-task",
        event_id="live-event",
        event_type="input.normalized",
        category="input",
        summary={},
        idempotency_key="live-key",
        artifact=_artifact(),
    )
    restarted.cleanup_trace("live-task")
    with pytest.raises(ValueError, match="trace_not_found"):
        restarted.trace("live-task")


def test_finding_trace_selects_only_its_work_unit_and_is_cleaned_up(
    tmp_path: Path,
) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")
    store.append_trace_event(
        task_id="task-1",
        event_id="input-1",
        event_type="input.normalized",
        category="input",
        summary={"file_count": 2},
        idempotency_key="input-1",
    )
    for unit_id in ("unit-1", "unit-2"):
        store.append_trace_event(
            task_id="task-1",
            event_id=f"model-{unit_id}",
            event_type="model.call_succeeded",
            category="model",
            summary={"model_call_id": f"call-{unit_id}", "work_unit_id": unit_id},
            idempotency_key=f"model-{unit_id}",
        )
    store.append_trace_event(
        task_id="task-1",
        event_id="finding-1",
        event_type="finding.validated",
        category="finding",
        summary={
            "finding_id": "finding-1",
            "trace_id": "comment-trace-1",
            "work_unit_id": "unit-1",
        },
        idempotency_key="finding-1",
    )
    store.link_finding_trace(
        trace_id="comment-trace-1",
        task_id="task-1",
        finding_id="finding-1",
        work_unit_ids=("unit-1",),
    )

    selected = store.trace("comment-trace-1")

    assert [event.event_id for event in selected] == [
        "input-1",
        "model-unit-1",
        "finding-1",
    ]
    store.cleanup_trace("task-1")
    with pytest.raises(ValueError, match="trace_not_found"):
        store.trace("comment-trace-1")


def test_finding_trace_expires_with_its_earliest_task_event(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    store.append_trace_event(
        task_id="task-expiring",
        event_id="expired-model",
        event_type="model.call_succeeded",
        category="model",
        summary={"model_call_id": "call-1", "work_unit_id": "unit-1"},
        idempotency_key="expired-model",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    store.link_finding_trace(
        trace_id="comment-expiring",
        task_id="task-expiring",
        finding_id="finding-1",
        work_unit_ids=("unit-1",),
    )

    restarted = ReviewStateStore(database)

    with pytest.raises(ValueError, match="trace_not_found"):
        restarted.trace("comment-expiring")
