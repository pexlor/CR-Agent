import pytest

from code_review_agent.domain.trace.models import (
    FactKind,
    TraceCategory,
    TraceEdge,
    TraceEventDraft,
)
from code_review_agent.domain.trace.service import TraceService


def event(event_type: str, key: str, summary: dict[str, object]) -> TraceEventDraft:
    category_name = event_type.split(".", 1)[0]
    category = TraceCategory(category_name)
    return TraceEventDraft(
        event_type=event_type,
        category=category,
        fact_kind=FactKind.OBSERVATION,
        producer="test",
        summary=summary,
        idempotency_key=key,
    )


def test_events_get_contiguous_task_sequence_and_idempotent_replay() -> None:
    service = TraceService()
    first = service.prepare_events(
        "task-1",
        "task_creation",
        [event("task.created", "create", {"status": "created"})],
    )
    committed = service.commit(first)
    replay = service.commit(first)

    assert committed[0].sequence == 1
    assert replay == committed
    second = service.commit(
        service.prepare_events(
            "task-1",
            "execution",
            [event("task.execution_started", "run", {"status": "running"})],
        )
    )
    assert second[0].sequence == 2

    conflict = service.prepare_events(
        "task-1",
        "execution",
        [event("task.execution_started", "run", {"status": "different"})],
    )
    with pytest.raises(ValueError):
        service.commit(conflict)


def test_edges_are_same_task_and_events_are_append_only() -> None:
    service = TraceService()
    created = service.commit(
        service.prepare_events(
            "task-1",
            "creation",
            [event("task.created", "create", {"status": "created"})],
        )
    )[0]
    with pytest.raises(ValueError):
        service.add_edge(
            TraceEdge(
                edge_id="edge-1",
                task_id="task-1",
                source_type="event",
                source_id=created.event_id,
                target_type="event",
                target_id="other",
                relation="caused_by",
                created_event_id=created.event_id,
                idempotency_key="edge",
                source_task_id="task-1",
                target_task_id="task-2",
            )
        )


def test_comment_link_requires_direct_evidence_and_one_current_link() -> None:
    service = TraceService()
    with pytest.raises(ValueError):
        service.create_comment_link(
            task_id="task-1",
            finding_id="finding-1",
            direct_evidence_ids=(),
            shared_process_ids=("model-1",),
            validation_event_ids=("validation-1",),
            checkpoint_id="checkpoint-1",
        )

    link = service.create_comment_link(
        task_id="task-1",
        finding_id="finding-1",
        direct_evidence_ids=("evidence-1",),
        shared_process_ids=("model-1",),
        validation_event_ids=("validation-1",),
        checkpoint_id="checkpoint-1",
    )
    assert link.link_version == 1
    with pytest.raises(ValueError):
        service.create_comment_link(
            task_id="task-1",
            finding_id="finding-1",
            direct_evidence_ids=("evidence-2",),
            shared_process_ids=(),
            validation_event_ids=(),
            checkpoint_id="checkpoint-2",
        )
