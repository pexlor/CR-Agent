from datetime import UTC, datetime

import pytest

from code_review_agent.domain.trace.models import (
    FactKind,
    TraceCategory,
    TraceEdge,
    TraceEventDraft,
    TraceEventEnvelope,
    TraceLink,
)


def draft(**overrides: object) -> TraceEventDraft:
    values: dict[str, object] = {
        "event_type": "task.created",
        "category": TraceCategory.TASK,
        "fact_kind": FactKind.OBSERVATION,
        "producer": "task",
        "summary": {"status": "created"},
    }
    values.update(overrides)
    return TraceEventDraft(**values)  # type: ignore[arg-type]


def test_event_draft_rejects_free_text_reasoning_and_non_scalar_summary() -> None:
    with pytest.raises(ValueError):
        draft(summary={"reasoning": "private chain of thought"})
    with pytest.raises(TypeError):
        draft(summary={"nested": {"raw": "response"}})


def test_envelope_and_link_are_immutable_and_link_requires_direct_evidence() -> None:
    envelope = TraceEventEnvelope(
        event_id="event-1",
        task_id="task-1",
        sequence=1,
        event_type="task.created",
        event_version=1,
        category=TraceCategory.TASK,
        fact_kind=FactKind.OBSERVATION,
        occurred_at=datetime.now(UTC),
        recorded_at=datetime.now(UTC),
        producer="task",
        correlation_id="task-1",
        summary={"status": "created"},
        idempotency_key="create-1",
        retention_group_id="retention-1",
        schema_digest="schema",
    )
    with pytest.raises(AttributeError):
        envelope.sequence = 2  # type: ignore[misc]
    with pytest.raises(ValueError):
        TraceLink(
            trace_id="trace-1",
            task_id="task-1",
            finding_id="finding-1",
            link_version=1,
            direct_evidence_ids=(),
            shared_process_ids=(),
            validation_event_ids=(),
            confidence_event_id=None,
            budget_ledger_version=1,
            checkpoint_id="checkpoint-1",
            created_event_id=envelope.event_id,
        )


def test_edge_rejects_cross_task_reference() -> None:
    with pytest.raises(ValueError):
        TraceEdge(
            edge_id="edge-1",
            task_id="task-1",
            source_type="event",
            source_id="event-1",
            target_type="event",
            target_id="event-2",
            relation="caused_by",
            created_event_id="event-1",
            idempotency_key="edge-1",
            source_task_id="task-1",
            target_task_id="task-2",
        )
