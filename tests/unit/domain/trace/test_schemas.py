import pytest

from code_review_agent.domain.trace.models import FactKind, TraceCategory, TraceEventDraft
from code_review_agent.domain.trace.schemas import TraceSchemaRegistry


def event(event_type: str, summary: dict[str, object]) -> TraceEventDraft:
    return TraceEventDraft(
        event_type=event_type,
        category=TraceCategory.TOOL if event_type.startswith("tool.") else TraceCategory.MODEL,
        fact_kind=FactKind.OBSERVATION,
        producer="test",
        summary=summary,
    )


def test_standard_event_types_are_registered_with_versions() -> None:
    registry = TraceSchemaRegistry.standard()

    assert registry.get("task.created").version == 1
    assert registry.get("model.call_unknown").version == 1
    assert registry.get("tool.attempt_succeeded").version == 1


def test_tool_success_requires_started_and_model_success_requires_succeeded() -> None:
    registry = TraceSchemaRegistry.standard()

    with pytest.raises(ValueError):
        registry.validate(event("tool.attempt_succeeded", {"status": "succeeded"}), ())
    with pytest.raises(ValueError):
        registry.validate(event("model.call_succeeded", {"provider_state": "unknown"}), ())

    started = event("tool.attempt_started", {"status": "running"})
    succeeded = event("tool.attempt_succeeded", {"status": "succeeded"})
    assert registry.validate(succeeded, (started,)) is None


def test_unknown_model_cannot_become_succeeded_by_schema_validation() -> None:
    registry = TraceSchemaRegistry.standard()
    unknown = event("model.call_unknown", {"provider_state": "unknown"})

    with pytest.raises(ValueError):
        registry.validate(event("model.call_succeeded", {"provider_state": "unknown"}), (unknown,))
