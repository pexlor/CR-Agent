"""Trace intent preparation and read-only in-memory projection."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.trace.models import (
    TraceEdge,
    TraceEventDraft,
    TraceEventEnvelope,
    TraceIntegrityResult,
    TraceLink,
    TraceMutationIntent,
    TraceTimelineView,
)
from code_review_agent.domain.trace.schemas import TraceSchemaRegistry


class TraceService:
    """Create append-only Trace intents and maintain a deterministic read projection."""

    def __init__(self, registry: TraceSchemaRegistry | None = None) -> None:
        self._registry = registry or TraceSchemaRegistry.standard()
        self._events: dict[str, list[TraceEventEnvelope]] = {}
        self._edges: dict[str, list[TraceEdge]] = {}
        self._links: dict[tuple[str, str], TraceLink] = {}
        self._intent_results: dict[str, tuple[TraceEventEnvelope, ...]] = {}
        self._idempotency: dict[
            tuple[str, str], tuple[str, tuple[TraceEventEnvelope, ...]]
        ] = {}

    def prepare_events(
        self,
        task_id: str,
        business_transaction_kind: str,
        facts: list[TraceEventDraft] | tuple[TraceEventDraft, ...],
        *,
        expected_task_version: int | None = None,
        lease_id: str | None = None,
        fencing_token: int | None = None,
        idempotency_key: str | None = None,
    ) -> TraceMutationIntent:
        if not task_id or not business_transaction_kind or not facts:
            raise ValueError("trace intent requires task, transaction and facts")
        normalized: list[TraceEventDraft] = []
        for index, fact in enumerate(facts, start=1):
            key = fact.idempotency_key or f"{idempotency_key or uuid4()}:{index}"
            normalized.append(replace(fact, idempotency_key=key))
        return TraceMutationIntent(
            intent_id=str(uuid4()),
            task_id=task_id,
            business_transaction_kind=business_transaction_kind,
            events=tuple(normalized),
            expected_task_version=expected_task_version,
            lease_id=lease_id,
            fencing_token=fencing_token,
            idempotency_key=idempotency_key
            or normalized[0].idempotency_key
            or str(uuid4()),
        )

    def commit(self, intent: TraceMutationIntent) -> tuple[TraceEventEnvelope, ...]:
        prior = self._intent_results.get(intent.intent_id)
        if prior is not None:
            return prior
        keys = [event.idempotency_key for event in intent.events]
        if any(key is None for key in keys):
            raise ValueError("trace intent event idempotency is required")
        concrete_keys = [key for key in keys if key is not None]
        existing_results: list[tuple[TraceEventEnvelope, ...]] = []
        for key in concrete_keys:
            prior_by_key = self._idempotency.get((intent.task_id, key))
            if prior_by_key is not None:
                if prior_by_key[0] != intent.content_digest:
                    raise ValueError("trace_idempotency_conflict")
                existing_results.append(prior_by_key[1])
        if existing_results:
            if len(existing_results) != len(concrete_keys):
                raise ValueError("trace_idempotency_conflict")
            result = existing_results[0]
            self._intent_results[intent.intent_id] = result
            return result

        task_events = self._events.setdefault(intent.task_id, [])
        predecessors: list[TraceEventEnvelope | TraceEventDraft] = list(task_events)
        committed: list[TraceEventEnvelope] = []
        now = datetime.now(UTC)
        for offset, fact in enumerate(intent.events, start=1):
            self._registry.validate(fact, tuple(predecessors))
            if fact.causation_event_id is not None and not any(
                item.event_id == fact.causation_event_id
                for item in predecessors
                if isinstance(item, TraceEventEnvelope)
            ):
                raise ValueError("trace_causation_missing")
            envelope = TraceEventEnvelope(
                event_id=str(uuid4()),
                task_id=intent.task_id,
                sequence=len(task_events) + offset,
                event_type=fact.event_type,
                event_version=fact.event_version,
                category=fact.category,
                fact_kind=fact.fact_kind,
                occurred_at=now,
                recorded_at=now,
                producer=fact.producer,
                correlation_id=fact.correlation_id or intent.task_id,
                causation_event_id=fact.causation_event_id,
                work_unit_id=fact.work_unit_id,
                attempt_id=fact.attempt_id,
                model_call_id=fact.model_call_id,
                tool_call_id=fact.tool_call_id,
                checkpoint_id=fact.checkpoint_id,
                payload_ref=fact.payload_ref,
                summary=fact.summary,
                idempotency_key=fact.idempotency_key or str(uuid4()),
                retention_group_id=intent.task_id,
                schema_digest=sha256_digest(
                    {"event_type": fact.event_type, "event_version": fact.event_version}
                ),
            )
            committed.append(envelope)
            predecessors.append(envelope)
        task_events.extend(committed)
        result = tuple(committed)
        self._intent_results[intent.intent_id] = result
        for key in concrete_keys:
            self._idempotency[(intent.task_id, key)] = (intent.content_digest, result)
        return result

    def add_edge(self, edge: TraceEdge) -> TraceEdge:
        if edge.task_id not in self._events:
            raise ValueError("trace_causation_missing")
        event_ids = {event.event_id for event in self._events[edge.task_id]}
        for object_type, object_id in (
            (edge.source_type, edge.source_id),
            (edge.target_type, edge.target_id),
        ):
            if object_type == "event" and object_id not in event_ids:
                raise ValueError("trace_causation_missing")
        if any(
            item.idempotency_key == edge.idempotency_key
            for item in self._edges[edge.task_id]
        ):
            return next(
                item
                for item in self._edges[edge.task_id]
                if item.idempotency_key == edge.idempotency_key
            )
        self._edges[edge.task_id].append(edge)
        return edge

    def create_comment_link(
        self,
        *,
        task_id: str,
        finding_id: str,
        direct_evidence_ids: tuple[str, ...],
        shared_process_ids: tuple[str, ...],
        validation_event_ids: tuple[str, ...],
        checkpoint_id: str,
        confidence_event_id: str | None = None,
        budget_ledger_version: int = 0,
    ) -> TraceLink:
        key = (task_id, finding_id)
        if key in self._links:
            raise ValueError("trace link already exists")
        link = TraceLink(
            trace_id=str(uuid4()),
            task_id=task_id,
            finding_id=finding_id,
            link_version=1,
            direct_evidence_ids=direct_evidence_ids,
            shared_process_ids=shared_process_ids,
            validation_event_ids=validation_event_ids,
            confidence_event_id=confidence_event_id,
            budget_ledger_version=budget_ledger_version,
            checkpoint_id=checkpoint_id,
            created_event_id="pending",
        )
        self._links[key] = link
        return link

    def get_task_timeline(self, task_id: str) -> TraceTimelineView:
        return TraceTimelineView(
            task_id=task_id,
            events=tuple(self._events.get(task_id, ())),
            edges=tuple(self._edges.get(task_id, ())),
        )

    def get_comment_trace(self, trace_id: str) -> TraceLink:
        """Return the current or historical immutable link by trace ID."""

        for link in self._links.values():
            if link.trace_id == trace_id:
                return link
        raise ValueError("trace_not_found")

    def verify_integrity(self, task_id: str) -> TraceIntegrityResult:
        events = self._events.get(task_id, [])
        if any(event.sequence != index for index, event in enumerate(events, start=1)):
            return TraceIntegrityResult(
                task_id, False, "trace_integrity_error", len(events), 0
            )
        event_ids = {event.event_id for event in events}
        for edge in self._edges.get(task_id, []):
            if (
                edge.task_id != task_id
                or (edge.source_type == "event" and edge.source_id not in event_ids)
                or (edge.target_type == "event" and edge.target_id not in event_ids)
            ):
                return TraceIntegrityResult(
                    task_id, False, "trace_integrity_error", len(events), 0
                )
        links = [
            link for (link_task, _), link in self._links.items() if link_task == task_id
        ]
        if any(not link.direct_evidence_ids for link in links):
            return TraceIntegrityResult(
                task_id, False, "trace_integrity_error", len(events), len(links)
            )
        return TraceIntegrityResult(task_id, True, None, len(events), len(links))
