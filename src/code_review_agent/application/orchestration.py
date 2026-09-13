"""Deterministic application orchestration for one local review run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from code_review_agent.application.dto import (
    ReviewProgressView,
    ReviewRunResult,
    StartReviewCommand,
    TraceEventView,
)


class SessionPhase(StrEnum):
    SESSION_ACQUIRED = "session_acquired"
    INPUT_NORMALIZING = "input_normalizing"
    PLANNING = "planning"
    EXECUTING = "executing"
    CONSOLIDATING = "consolidating"
    SNAPSHOTTING = "snapshotting"
    REPORTING = "reporting"
    COMPLETED = "completed"


_PHASE_ORDER = tuple(SessionPhase)


@dataclass(slots=True)
class ExecutionSession:
    task_id: str
    session_id: str = field(default_factory=lambda: str(uuid4()))
    phase: SessionPhase = SessionPhase.SESSION_ACQUIRED
    _trace: list[TraceEventView] = field(default_factory=list)

    def advance(self, target: SessionPhase) -> None:
        current_index = _PHASE_ORDER.index(self.phase)
        target_index = _PHASE_ORDER.index(target)
        if target_index <= current_index:
            raise ValueError("illegal_phase_transition")
        if target_index != current_index + 1:
            raise ValueError("illegal_phase_transition")
        self.phase = target
        self._trace.append(
            TraceEventView(len(self._trace) + 1, target.value, "phase advanced")
        )

    @property
    def trace(self) -> tuple[TraceEventView, ...]:
        return tuple(self._trace)


class ReviewSteps(Protocol):
    def normalize(self, command: StartReviewCommand) -> Any: ...

    def plan(self, normalized: Any) -> Any: ...

    async def execute(self, unit: Any) -> Any: ...

    def consolidate(
        self, normalized: Any, plan: Any, executions: tuple[Any, ...]
    ) -> Any: ...

    def snapshot(
        self,
        command: StartReviewCommand,
        normalized: Any,
        plan: Any,
        executions: tuple[Any, ...],
        finding_set: Any,
    ) -> Any: ...

    def report(self, snapshot: Any) -> Any: ...

    def deliver(self, model: Any, target: Path) -> Any: ...


@dataclass(frozen=True, slots=True)
class ReviewDependencies:
    steps: ReviewSteps
    should_stop: Callable[[], bool] = lambda: False
    checkpoint_store: Any | None = None
    rules_config_digest: str = ""
    resume: bool = False
    confirm_unknown_retry: bool = False


class ReviewOrchestrator:
    """Owns ordering and stop boundaries, not domain review decisions."""

    async def run(
        self,
        command: StartReviewCommand,
        dependencies: ReviewDependencies,
    ) -> ReviewRunResult:
        session = ExecutionSession(command.task_id)
        normalized = dependencies.steps.normalize(command)
        session.advance(SessionPhase.INPUT_NORMALIZING)
        input_digest = self._input_digest(command, normalized)
        digest_factory = getattr(dependencies.steps, "checkpoint_binding_digest", None)
        rules_config_digest = dependencies.rules_config_digest or (
            digest_factory(command)
            if callable(digest_factory)
            else self._rules_digest(dependencies.steps, command)
        )
        checkpoint_failed = False
        completed: dict[str, Any] = {}
        if dependencies.checkpoint_store is not None:
            try:
                checkpoint = dependencies.checkpoint_store.load_checkpoint(
                    command.task_id,
                    input_digest=input_digest,
                    rules_config_digest=rules_config_digest,
                )
                completed = dict(checkpoint.completed_units)
            except ValueError as exc:
                if str(exc) != "checkpoint_not_found" or dependencies.resume:
                    raise
                try:
                    dependencies.checkpoint_store.save_checkpoint(
                        task_id=command.task_id,
                        input_digest=input_digest,
                        rules_config_digest=rules_config_digest,
                        kind="input_normalized",
                    )
                except Exception:
                    checkpoint_failed = True
        plan = dependencies.steps.plan(normalized)
        session.advance(SessionPhase.PLANNING)

        executions: list[Any] = []
        session.advance(SessionPhase.EXECUTING)
        stop_requested = False
        unknown_execution = False
        for unit in sorted(plan.work_units, key=lambda item: item.execution_rank):
            work_unit_id = str(
                getattr(unit, "work_unit_id", f"rank:{unit.execution_rank}")
            )
            if work_unit_id in completed:
                executions.append(completed[work_unit_id])
                continue
            if dependencies.should_stop():
                stop_requested = True
                break
            execution = await dependencies.steps.execute(unit)
            executions.append(execution)
            state = getattr(execution, "state", None)
            is_unknown = getattr(state, "value", state) == "unknown"
            if dependencies.checkpoint_store is not None:
                try:
                    dependencies.checkpoint_store.save_checkpoint(
                        task_id=command.task_id,
                        input_digest=input_digest,
                        rules_config_digest=rules_config_digest,
                        kind=(
                            "review_unit_unknown"
                            if is_unknown
                            else "review_unit_completed"
                        ),
                        work_unit_id=work_unit_id,
                        execution=None if is_unknown else execution,
                        unknown_execution=execution if is_unknown else None,
                    )
                except Exception:
                    checkpoint_failed = True
            if is_unknown:
                unknown_execution = True
                break

        session.advance(SessionPhase.CONSOLIDATING)
        finding_set = dependencies.steps.consolidate(
            normalized, plan, tuple(executions)
        )
        session.advance(SessionPhase.SNAPSHOTTING)
        snapshot = dependencies.steps.snapshot(
            command, normalized, plan, tuple(executions), finding_set
        )
        model = dependencies.steps.report(snapshot)
        session.advance(SessionPhase.REPORTING)
        delivery = dependencies.steps.deliver(model, command.output_path)
        session.advance(SessionPhase.COMPLETED)
        state = (
            "unknown"
            if unknown_execution
            else str(getattr(snapshot, "result_state", "complete_no_findings"))
        )
        if checkpoint_failed:
            state = "partial"
        path = getattr(delivery, "path", command.output_path)
        digest = getattr(delivery, "content_digest", None)
        return ReviewRunResult(
            task_id=command.task_id,
            session_id=session.session_id,
            phase=session.phase.value,
            result_state=state,
            delivery_state="succeeded",
            report_path=path,
            report_digest=digest,
            limitations=(
                ("checkpoint_persistence_failed",)
                if checkpoint_failed
                else ("unknown_execution",)
                if unknown_execution
                else ("stop_requested",)
                if stop_requested
                else ()
            ),
            trace=session.trace,
        )

    @staticmethod
    def _input_digest(command: StartReviewCommand, normalized: Any) -> str:
        direct = getattr(normalized, "input_digest", None)
        if isinstance(direct, str) and direct:
            return direct
        binding = getattr(normalized, "binding", None)
        bound = getattr(binding, "content_digest", None)
        if isinstance(bound, str) and bound:
            return bound
        source = command.diff_text
        if source is None and command.diff_file is not None:
            source = command.diff_file.read_text(encoding="utf-8")
        if source is None:
            source = command.source_url or ""
        return hashlib.sha256(source.encode()).hexdigest()

    @staticmethod
    def _rules_digest(steps: ReviewSteps, command: StartReviewCommand) -> str:
        config = getattr(steps, "config", None)
        payload = {
            "provider": command.provider,
            "model": command.model,
            "ruleset_id": getattr(config, "ruleset_id", ""),
            "ruleset_version": getattr(config, "ruleset_version", ""),
            "security_policy_id": getattr(config, "security_policy_id", ""),
            "security_policy_version": getattr(config, "security_policy_version", 0),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def progress(
        self,
        result: ReviewRunResult,
        trace: tuple[TraceEventView, ...],
    ) -> ReviewProgressView:
        return ReviewProgressView(
            task_id=result.task_id,
            phase=result.phase,
            result_state=result.result_state,
            delivery_state=result.delivery_state,
            trace=tuple(trace),
            publication_state=(
                result.publication.state if result.publication is not None else None
            ),
        )
