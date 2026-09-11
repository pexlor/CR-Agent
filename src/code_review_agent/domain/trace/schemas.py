"""Versioned Trace event schemas and authenticity checks."""

from __future__ import annotations

from dataclasses import dataclass

from code_review_agent.domain.trace.models import (
    TraceCategory,
    TraceEventDraft,
    TraceEventEnvelope,
)


@dataclass(frozen=True, slots=True)
class TraceEventSchema:
    event_type: str
    version: int
    category: TraceCategory
    required_predecessors: tuple[str, ...] = ()


_STANDARD_EVENT_TYPES: tuple[tuple[str, TraceCategory], ...] = (
    ("task.created", TraceCategory.TASK),
    ("task.execution_started", TraceCategory.TASK),
    ("task.stop_requested", TraceCategory.TASK),
    ("task.stop_observed", TraceCategory.TASK),
    ("task.paused", TraceCategory.TASK),
    ("task.resume_started", TraceCategory.TASK),
    ("task.resumed", TraceCategory.TASK),
    ("task.terminated", TraceCategory.TASK),
    ("input.acquire_started", TraceCategory.INPUT),
    ("input.acquired", TraceCategory.INPUT),
    ("input.rejected", TraceCategory.INPUT),
    ("input.normalized", TraceCategory.INPUT),
    ("input.scope_skipped", TraceCategory.INPUT),
    ("security.artifact_sanitized", TraceCategory.SECURITY),
    ("security.artifact_blocked", TraceCategory.SECURITY),
    ("security.boundary_failed", TraceCategory.SECURITY),
    ("security.attestation_revalidated", TraceCategory.SECURITY),
    ("planning.plan_created", TraceCategory.PLANNING),
    ("planning.work_unit_created", TraceCategory.PLANNING),
    ("tool.attempt_prepared", TraceCategory.TOOL),
    ("tool.attempt_started", TraceCategory.TOOL),
    ("tool.attempt_succeeded", TraceCategory.TOOL),
    ("tool.attempt_failed", TraceCategory.TOOL),
    ("tool.attempt_blocked", TraceCategory.TOOL),
    ("tool.attempt_interrupted", TraceCategory.TOOL),
    ("model.call_reserved", TraceCategory.MODEL),
    ("model.call_started", TraceCategory.MODEL),
    ("model.call_succeeded", TraceCategory.MODEL),
    ("model.call_failed_known", TraceCategory.MODEL),
    ("model.call_unknown", TraceCategory.MODEL),
    ("model.response_accepted", TraceCategory.MODEL),
    ("model.response_rejected", TraceCategory.MODEL),
    ("budget.authorization_initial", TraceCategory.BUDGET),
    ("budget.reservation_created", TraceCategory.BUDGET),
    ("budget.reservation_released", TraceCategory.BUDGET),
    ("budget.usage_settled", TraceCategory.BUDGET),
    ("budget.unknown_committed", TraceCategory.BUDGET),
    ("budget.usage_overage_recorded", TraceCategory.BUDGET),
    ("budget.freeze_applied", TraceCategory.BUDGET),
    ("budget.authorization_added", TraceCategory.BUDGET),
    ("budget.freeze_cleared", TraceCategory.BUDGET),
    ("budget.account_closed", TraceCategory.BUDGET),
    ("finding.candidate_created", TraceCategory.FINDING),
    ("finding.candidate_rejected", TraceCategory.FINDING),
    ("finding.validated", TraceCategory.FINDING),
    ("finding.merged", TraceCategory.FINDING),
    ("finding.confidence_assigned", TraceCategory.FINDING),
    ("comment.finalized", TraceCategory.COMMENT),
    ("comment.trace_link_created", TraceCategory.COMMENT),
    ("comment.superseded", TraceCategory.COMMENT),
    ("checkpoint.created", TraceCategory.CHECKPOINT),
    ("checkpoint.reused", TraceCategory.CHECKPOINT),
    ("checkpoint.reconciled", TraceCategory.CHECKPOINT),
    ("report.generation_started", TraceCategory.REPORT),
    ("report.delivered", TraceCategory.REPORT),
    ("report.delivery_failed", TraceCategory.REPORT),
    ("report.delivery_unknown", TraceCategory.REPORT),
    ("report.delivery_reconciled", TraceCategory.REPORT),
    ("trace.event_corrected", TraceCategory.CORRECTION),
    ("trace.event_revoked", TraceCategory.CORRECTION),
    ("trace.link_corrected", TraceCategory.CORRECTION),
)


class TraceSchemaRegistry:
    """Registry of fixed event schemas."""

    def __init__(self, schemas: dict[str, TraceEventSchema]) -> None:
        self._schemas = dict(schemas)

    @classmethod
    def standard(cls) -> TraceSchemaRegistry:
        schemas = {
            event_type: TraceEventSchema(event_type, 1, category)
            for event_type, category in _STANDARD_EVENT_TYPES
        }
        schemas["tool.attempt_succeeded"] = TraceEventSchema(
            "tool.attempt_succeeded", 1, TraceCategory.TOOL, ("tool.attempt_started",)
        )
        schemas["model.call_succeeded"] = TraceEventSchema(
            "model.call_succeeded", 1, TraceCategory.MODEL, ("model.call_started",)
        )
        schemas["model.response_accepted"] = TraceEventSchema(
            "model.response_accepted", 1, TraceCategory.MODEL, ("model.call_succeeded",)
        )
        schemas["model.response_rejected"] = TraceEventSchema(
            "model.response_rejected", 1, TraceCategory.MODEL, ("model.call_succeeded",)
        )
        return cls(schemas)

    def get(self, event_type: str) -> TraceEventSchema:
        try:
            return self._schemas[event_type]
        except KeyError as exc:
            raise ValueError("trace_event_invalid") from exc

    def validate(
        self,
        event: TraceEventDraft | TraceEventEnvelope,
        predecessors: tuple[TraceEventDraft | TraceEventEnvelope, ...],
    ) -> None:
        schema = self.get(event.event_type)
        if (
            event.event_version != schema.version
            or event.category is not schema.category
        ):
            raise ValueError("trace_event_invalid")
        predecessor_types = {item.event_type for item in predecessors}
        for required in schema.required_predecessors:
            if required not in predecessor_types:
                raise ValueError("trace_causation_missing")
        status = event.summary.get("status")
        provider_state = event.summary.get("provider_state")
        if event.event_type == "tool.attempt_succeeded" and status != "succeeded":
            raise ValueError("trace_event_invalid")
        if event.event_type == "model.call_succeeded" and provider_state != "succeeded":
            raise ValueError("trace_event_invalid")
        if event.event_type == "model.call_unknown" and provider_state != "unknown":
            raise ValueError("trace_event_invalid")
        if event.event_type == "tool.attempt_started" and status != "running":
            raise ValueError("trace_event_invalid")
        if event.event_type in {"model.response_accepted", "model.response_rejected"}:
            if (
                event.event_type == "model.response_accepted"
                and event.summary.get("response_state") != "accepted"
            ):
                raise ValueError("trace_event_invalid")
            if event.event_type == "model.response_rejected" and event.summary.get(
                "response_state"
            ) not in {
                "invalid_output",
                "security_rejected",
            }:
                raise ValueError("trace_event_invalid")
