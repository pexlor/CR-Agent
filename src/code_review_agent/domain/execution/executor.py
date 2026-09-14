"""Pipeline: fixed WorkUnit -> tools -> PromptEnvelope -> model -> candidates."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import uuid4

from code_review_agent.adapters.model.gateway import ModelGateway
from code_review_agent.domain.budget.models import (
    BudgetReservation,
    NormalizedActualUsage,
    ProviderHardBudgetCapability,
    ReservationDenied,
)
from code_review_agent.domain.budget.models import (
    UsageState as BudgetUsageState,
)
from code_review_agent.domain.budget.service import BudgetService
from code_review_agent.domain.common.digests import canonical_json, sha256_digest
from code_review_agent.domain.execution.execution_models import (
    CandidateFinding,
    CandidateLocation,
    CandidateLocationKind,
    CoverageImpact,
    EvidenceReference,
    ModelAttemptOutcomeKind,
    ModelCallAttempt,
    ToolAttempt,
    ToolAttemptState,
    WorkUnitExecutionResult,
    WorkUnitExecutionState,
)
from code_review_agent.domain.execution.models import (
    ModelCallOutcome,
    ModelRequestOptions,
    PromptEnvelope,
    ProviderState,
    ReservationAction,
    ResponseState,
    StructuredOutputStrategy,
)
from code_review_agent.domain.planning.models import ToolFailureImpact, WorkUnit
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    SanitizedArtifactRef,
    TrustLabel,
)
from code_review_agent.domain.security.service import SecurityService
from code_review_agent.ports.model import ModelGatewayPort
from code_review_agent.ports.tools import (
    AuthorizedToolInput,
    RestrictedToolRuntimePort,
    ToolCatalogPort,
    ToolExecutionContext,
    ToolExecutionState,
    deterministic_tool_token_count,
)

_OUTPUT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "category",
                    "title",
                    "problem",
                    "trigger_condition",
                    "impact",
                    "suggestion",
                    "change_causation",
                    "limitations",
                ],
            },
        }
    },
}
_CANDIDATE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Fixed, immutable inputs to execute exactly one WorkUnit attempt."""

    task_id: str
    work_unit: WorkUnit
    attempt_number: int
    model_provider: ModelGatewayPort
    model_options_strategy: StructuredOutputStrategy
    max_output_tokens: int
    budget_service: BudgetService
    budget_account_id: str
    security_service: SecurityService
    tool_catalog: ToolCatalogPort
    system_rules: str
    diff_text: str
    tool_runtime: RestrictedToolRuntimePort | None = None
    execution_id: str = field(default_factory=lambda: str(uuid4()))
    model_call_guard: object | None = None

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("execution request task_id is required")
        if self.attempt_number <= 0:
            raise ValueError("execution request attempt_number must be positive")
        if self.max_output_tokens <= 0:
            raise ValueError("execution request max_output_tokens must be positive")
        if not self.execution_id:
            raise ValueError("execution request execution_id is required")


class WorkUnitExecutor:
    """Executes one fixed WorkUnit through tools, then at most one model call."""

    async def execute(self, request: ExecutionRequest) -> WorkUnitExecutionResult:
        tool_attempts, tool_facts, coverage_impact = self._run_tools(request)
        if coverage_impact is CoverageImpact.NOT_COVERED:
            return WorkUnitExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                work_unit_id=request.work_unit.work_unit_id,
                plan_id=request.work_unit.plan_id,
                attempt_number=request.attempt_number,
                state=WorkUnitExecutionState.PARTIAL,
                coverage_impact=coverage_impact,
                tool_attempts=tool_attempts,
                model_attempt=None,
                model_outcome_kind=ModelAttemptOutcomeKind.NOT_ATTEMPTED,
                candidates=(),
                error_code="tool_coverage_degraded",
            )

        envelope = self._build_envelope(request, tool_facts)
        gateway = ModelGateway()
        options = ModelRequestOptions(
            strategy=request.model_options_strategy,
            max_output_tokens=request.max_output_tokens,
        )
        try:
            prepared = gateway.prepare(request.model_provider, envelope, options)
        except Exception:
            return WorkUnitExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                work_unit_id=request.work_unit.work_unit_id,
                plan_id=request.work_unit.plan_id,
                attempt_number=request.attempt_number,
                state=WorkUnitExecutionState.FAILED,
                coverage_impact=coverage_impact,
                tool_attempts=tool_attempts,
                model_attempt=None,
                model_outcome_kind=ModelAttemptOutcomeKind.NOT_ATTEMPTED,
                candidates=(),
                error_code="model_prepare_failed",
            )

        if not self._egress_is_safe(request, prepared.body):
            gateway.discard_prepared(request.model_provider, prepared)
            return WorkUnitExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                work_unit_id=request.work_unit.work_unit_id,
                plan_id=request.work_unit.plan_id,
                attempt_number=request.attempt_number,
                state=WorkUnitExecutionState.FAILED,
                coverage_impact=coverage_impact,
                tool_attempts=tool_attempts,
                model_attempt=None,
                model_outcome_kind=ModelAttemptOutcomeKind.NOT_ATTEMPTED,
                candidates=(),
                error_code="security_boundary_failed",
            )

        request_ref = self._prepare_trace_artifact(
            request,
            prepared.body.decode("utf-8", errors="replace"),
            artifact_id=f"trace-model-request-{request.execution_id}",
            purpose=ArtifactPurpose.TRACE_MODEL_REQUEST,
            source=ArtifactSource.TRUSTED_APPLICATION,
        )
        if request_ref is None:
            gateway.discard_prepared(request.model_provider, prepared)
            return WorkUnitExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                work_unit_id=request.work_unit.work_unit_id,
                plan_id=request.work_unit.plan_id,
                attempt_number=request.attempt_number,
                state=WorkUnitExecutionState.PARTIAL,
                coverage_impact=CoverageImpact.DEGRADED,
                tool_attempts=tool_attempts,
                model_attempt=None,
                model_outcome_kind=ModelAttemptOutcomeKind.NOT_ATTEMPTED,
                candidates=(),
                error_code="trace_persistence_failed",
            )

        capability = ProviderHardBudgetCapability(
            capability_id="capability-1",
            provider_id=prepared.provider_id,
            provider_version=prepared.provider_version,
            model_id=prepared.model_id,
            origin=prepared.origin,
            prepared_request_digest=prepared.body_digest,
            token_counted_request_digest=prepared.body_digest,
            max_output_enforced=True,
            usage_mapping_trusted=True,
            retries_disabled=True,
        )
        model_call_id = str(uuid4())
        reservation = request.budget_service.prepare_reservation(
            request.budget_account_id,
            model_call_id=model_call_id,
            work_unit_id=request.work_unit.work_unit_id,
            input_bound=prepared.input_token_bound,
            output_max=prepared.output_token_max,
            prepared_request_digest=prepared.body_digest,
            capability=capability,
        )
        if isinstance(reservation, ReservationDenied):
            gateway.discard_prepared(request.model_provider, prepared)
            return WorkUnitExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                work_unit_id=request.work_unit.work_unit_id,
                plan_id=request.work_unit.plan_id,
                attempt_number=request.attempt_number,
                state=WorkUnitExecutionState.FAILED,
                coverage_impact=coverage_impact,
                tool_attempts=tool_attempts,
                model_attempt=None,
                model_outcome_kind=ModelAttemptOutcomeKind.NOT_ATTEMPTED,
                candidates=(),
                error_code="budget_reservation_rejected",
            )

        guard = request.model_call_guard
        if guard is not None:
            try:
                guard.begin_model_call(  # type: ignore[attr-defined]
                    task_id=request.task_id,
                    model_call_id=model_call_id,
                    work_unit_id=request.work_unit.work_unit_id,
                    execution_id=request.execution_id,
                    reservation_id=reservation.reservation_id,
                    request_digest=prepared.body_digest,
                )
            except Exception:
                gateway.discard_prepared(request.model_provider, prepared)
                request.budget_service.release_unsent_reservation(
                    reservation.reservation_id
                )
                return WorkUnitExecutionResult(
                    execution_id=request.execution_id,
                    task_id=request.task_id,
                    work_unit_id=request.work_unit.work_unit_id,
                    plan_id=request.work_unit.plan_id,
                    attempt_number=request.attempt_number,
                    state=WorkUnitExecutionState.PARTIAL,
                    coverage_impact=CoverageImpact.DEGRADED,
                    tool_attempts=tool_attempts,
                    model_attempt=None,
                    model_outcome_kind=ModelAttemptOutcomeKind.NOT_ATTEMPTED,
                    candidates=(),
                    error_code="trace_persistence_failed",
                )

        outcome = await gateway.send(
            request.model_provider, prepared, reservation=reservation
        )
        self._settle(request.budget_service, reservation, outcome)
        if guard is not None:
            guard.finish_model_call(  # type: ignore[attr-defined]
                model_call_id,
                terminal_state=outcome.state.provider_state.value,
            )
        response_ref = None
        if outcome.response_payload is not None:
            response_ref = self._prepare_trace_artifact(
                request,
                canonical_json(dict(outcome.response_payload)),
                artifact_id=f"trace-model-response-{request.execution_id}",
                purpose=ArtifactPurpose.TRACE_MODEL_RESPONSE,
                source=ArtifactSource.MODEL_RESPONSE,
            )

        model_attempt = ModelCallAttempt(
            model_call_id=model_call_id,
            work_unit_id=request.work_unit.work_unit_id,
            execution_id=request.execution_id,
            provider_id=prepared.provider_id,
            model_id=prepared.model_id,
            request_ref=request_ref,
            response_ref=response_ref,
            reservation_id=reservation.reservation_id,
            outcome=outcome,
        )

        if outcome.state.provider_state is ProviderState.UNKNOWN:
            return WorkUnitExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                work_unit_id=request.work_unit.work_unit_id,
                plan_id=request.work_unit.plan_id,
                attempt_number=request.attempt_number,
                state=WorkUnitExecutionState.UNKNOWN,
                coverage_impact=coverage_impact,
                tool_attempts=tool_attempts,
                model_attempt=model_attempt,
                model_outcome_kind=ModelAttemptOutcomeKind.UNKNOWN,
                candidates=(),
                error_code=outcome.error_code,
            )
        if outcome.state.provider_state is ProviderState.FAILED_KNOWN:
            return WorkUnitExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                work_unit_id=request.work_unit.work_unit_id,
                plan_id=request.work_unit.plan_id,
                attempt_number=request.attempt_number,
                state=WorkUnitExecutionState.FAILED,
                coverage_impact=coverage_impact,
                tool_attempts=tool_attempts,
                model_attempt=model_attempt,
                model_outcome_kind=ModelAttemptOutcomeKind.FAILED_KNOWN,
                candidates=(),
                error_code=outcome.error_code,
            )

        candidates, evidence_items, response_state = self._process_response(
            request, outcome
        )
        if response_state is not ResponseState.ACCEPTED:
            final_coverage = (
                CoverageImpact.DEGRADED
                if coverage_impact is CoverageImpact.FULLY_COVERED
                else coverage_impact
            )
            return WorkUnitExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                work_unit_id=request.work_unit.work_unit_id,
                plan_id=request.work_unit.plan_id,
                attempt_number=request.attempt_number,
                state=WorkUnitExecutionState.SUCCEEDED,
                coverage_impact=final_coverage,
                tool_attempts=tool_attempts,
                model_attempt=model_attempt,
                model_outcome_kind=ModelAttemptOutcomeKind.SUCCEEDED,
                candidates=(),
                error_code=(
                    "model_output_invalid"
                    if response_state is ResponseState.INVALID_OUTPUT
                    else "security_boundary_failed"
                ),
            )

        return WorkUnitExecutionResult(
            execution_id=request.execution_id,
            task_id=request.task_id,
            work_unit_id=request.work_unit.work_unit_id,
            plan_id=request.work_unit.plan_id,
            attempt_number=request.attempt_number,
            state=WorkUnitExecutionState.SUCCEEDED,
            coverage_impact=coverage_impact,
            tool_attempts=tool_attempts,
            model_attempt=model_attempt,
            model_outcome_kind=ModelAttemptOutcomeKind.SUCCEEDED,
            candidates=candidates,
            evidence=evidence_items,
        )

    def _run_tools(
        self, request: ExecutionRequest
    ) -> tuple[tuple[ToolAttempt, ...], tuple[str, ...], CoverageImpact]:
        attempts: list[ToolAttempt] = []
        facts: list[str] = []
        coverage = CoverageImpact.FULLY_COVERED
        if not request.work_unit.tools:
            return (), (), coverage

        runtime = request.tool_runtime
        for selection in sorted(request.work_unit.tools, key=lambda item: item.order):
            declaration = request.tool_catalog.verify_fixed_reference(
                selection.fixed_reference
            )
            text = request.diff_text
            authorized_input = AuthorizedToolInput(
                text=text,
                path=request.work_unit.file_id,
                scope=f"work_unit:{request.work_unit.work_unit_id}",
                token_count=deterministic_tool_token_count(text),
            )
            context = ToolExecutionContext(
                task_id=request.task_id, execution_id=request.execution_id
            )
            if runtime is None:
                raise RuntimeError("tool selection requires a runtime")
            result = runtime.execute(
                selection.fixed_reference,
                authorized_input,
                selection.planned_limits,
                context,
            )
            rule_summary_digest = sha256_digest(sorted(selection.applicable_rule_ids))
            if result.state is ToolExecutionState.SUCCEEDED:
                attempts.append(
                    ToolAttempt(
                        tool_attempt_id=str(uuid4()),
                        work_unit_id=request.work_unit.work_unit_id,
                        execution_id=request.execution_id,
                        tool_id=declaration.tool_id,
                        tool_version=declaration.version,
                        rule_summary_digest=rule_summary_digest,
                        input_digest=result.input_digest,
                        state=ToolAttemptState.SUCCEEDED,
                        failure_impact=selection.failure_impact,
                        result=result,
                    )
                )
                for evidence in result.evidence:
                    facts.append(
                        f"{evidence.tool_id}:{evidence.rule_id}:{evidence.line}"
                    )
                continue

            attempts.append(
                ToolAttempt(
                    tool_attempt_id=str(uuid4()),
                    work_unit_id=request.work_unit.work_unit_id,
                    execution_id=request.execution_id,
                    tool_id=declaration.tool_id,
                    tool_version=declaration.version,
                    rule_summary_digest=rule_summary_digest,
                    input_digest=result.input_digest,
                    state=(
                        ToolAttemptState.BLOCKED
                        if result.state is ToolExecutionState.BLOCKED
                        else ToolAttemptState.FAILED_KNOWN
                    ),
                    failure_impact=selection.failure_impact,
                    error_code=result.error_code or "tool_failed",
                )
            )
            if selection.failure_impact is ToolFailureImpact.COVERAGE_DEGRADED:
                return tuple(attempts), tuple(facts), CoverageImpact.NOT_COVERED
            coverage = CoverageImpact.DEGRADED

        return tuple(attempts), tuple(facts), coverage

    def _build_envelope(
        self, request: ExecutionRequest, tool_facts: tuple[str, ...]
    ) -> PromptEnvelope:
        output_contract = (
            "Return exactly one JSON object with one top-level field: "
            '"findings". "findings" must always be an array; use an empty '
            "array when there are no valid findings. Do not return summary, "
            "markdown, prose, or any other top-level field. Write all "
            "human-readable finding content in Simplified Chinese, while "
            "keeping JSON property names exactly as defined by the schema."
        )
        return PromptEnvelope(
            system_rules=f"{request.system_rules} {output_contract}",
            work_unit=request.work_unit.work_unit_id,
            diff=request.diff_text,
            controlled_context=(),
            tool_facts=tool_facts or ("no_tool_evidence",),
            prohibited_capabilities=("dynamic_tools", "streaming", "retries"),
            version_digest=request.work_unit.fingerprint,
            output_schema=_OUTPUT_SCHEMA,
        )

    def _egress_is_safe(self, request: ExecutionRequest, body: bytes) -> bool:
        """Scan the final outbound request bytes before they leave the domain."""

        descriptor = ArtifactDescriptor(
            artifact_id=f"model-request-{request.execution_id}",
            task_id=request.task_id,
            source=ArtifactSource.TRUSTED_APPLICATION,
            trust_label=TrustLabel.UNTRUSTED_TEXT,
            kind=ArtifactKind.MODEL_PAYLOAD,
            purpose=ArtifactPurpose.MODEL_EGRESS,
        )
        text = body.decode("utf-8", errors="replace")
        prepared = None
        with suppress(Exception):
            prepared = request.security_service.evaluate_artifact(text, descriptor)
        return prepared is not None and prepared.decision.value in ("safe", "redacted")

    @staticmethod
    def _prepare_trace_artifact(
        request: ExecutionRequest,
        content: str,
        *,
        artifact_id: str,
        purpose: ArtifactPurpose,
        source: ArtifactSource,
    ) -> SanitizedArtifactRef | None:
        descriptor = ArtifactDescriptor(
            artifact_id=artifact_id,
            task_id=request.task_id,
            source=source,
            trust_label=TrustLabel.UNTRUSTED_TEXT,
            kind=ArtifactKind.TRACE_PAYLOAD,
            purpose=purpose,
        )
        try:
            prepared = request.security_service.evaluate_artifact(content, descriptor)
            return request.security_service.commit(prepared)
        except Exception:
            return None

    def _settle(
        self,
        budget_service: BudgetService,
        reservation: BudgetReservation,
        outcome: ModelCallOutcome,
    ) -> None:
        if outcome.reservation_action is ReservationAction.RELEASE:
            budget_service.release_unsent_reservation(reservation.reservation_id)
            return
        if outcome.reservation_action is ReservationAction.SETTLE_KNOWN:
            budget_service.settle_known_usage(
                reservation.reservation_id,
                NormalizedActualUsage(
                    input_tokens=outcome.usage.input_tokens or 0,
                    output_tokens=outcome.usage.output_tokens or 0,
                ),
            )
            return
        usage_state = (
            BudgetUsageState.MISSING
            if outcome.usage.state.value == "missing"
            else BudgetUsageState.UNTRUSTED
        )
        budget_service.settle_uncertain_usage(
            reservation.reservation_id,
            usage_state=usage_state,
            reported_usage=outcome.usage.reported_total,
        )

    def _process_response(
        self, request: ExecutionRequest, outcome: ModelCallOutcome
    ) -> tuple[
        tuple[CandidateFinding, ...], tuple[EvidenceReference, ...], ResponseState
    ]:
        payload = outcome.response_payload
        if payload is None:
            return (), (), ResponseState.INVALID_OUTPUT

        descriptor = ArtifactDescriptor(
            artifact_id=f"model-response-{request.execution_id}",
            task_id=request.task_id,
            source=ArtifactSource.MODEL_RESPONSE,
            trust_label=TrustLabel.UNTRUSTED_TEXT,
            kind=ArtifactKind.MODEL_PAYLOAD,
            purpose=ArtifactPurpose.DOMAIN_INGRESS,
        )
        response_text = canonical_json(dict(payload))
        prepared = None
        with suppress(Exception):
            prepared = request.security_service.evaluate_artifact(
                response_text, descriptor
            )
        if prepared is None or prepared.decision.value not in ("safe", "redacted"):
            return (), (), ResponseState.SECURITY_REJECTED

        findings = payload.get("findings")
        if not isinstance(findings, tuple):
            return (), (), ResponseState.INVALID_OUTPUT

        candidates: list[CandidateFinding] = []
        evidence_items: list[EvidenceReference] = []
        for index, raw in enumerate(findings, start=1):
            if not isinstance(raw, Mapping):
                return (), (), ResponseState.INVALID_OUTPUT
            required = (
                "category",
                "title",
                "problem",
                "trigger_condition",
                "impact",
                "suggestion",
                "change_causation",
                "limitations",
            )
            if any(field_name not in raw for field_name in required):
                return (), (), ResponseState.INVALID_OUTPUT
            model_evidence = EvidenceReference(
                evidence_id=f"evidence-{request.execution_id}-{index}",
                evidence_type="model_response",
                source_ref=request.execution_id,
                location=None,
                content_digest=sha256_digest(dict(raw)),
            )
            changed_code_evidence = EvidenceReference(
                evidence_id=f"changed-code-{request.execution_id}-{index}",
                evidence_type="changed_code",
                source_ref=request.work_unit.file_id,
                location=CandidateLocation(
                    kind=CandidateLocationKind.FILE,
                    file_id=request.work_unit.file_id,
                ),
                content_digest=sha256_digest(request.diff_text),
            )
            evidence_items.extend((model_evidence, changed_code_evidence))
            candidates.append(
                CandidateFinding(
                    candidate_id=f"candidate-{request.execution_id}-{index}",
                    task_id=request.task_id,
                    work_unit_id=request.work_unit.work_unit_id,
                    execution_id=request.execution_id,
                    category=str(raw["category"]),
                    title=str(raw["title"]),
                    problem=str(raw["problem"]),
                    trigger_condition=str(raw["trigger_condition"]),
                    location=CandidateLocation(
                        kind=CandidateLocationKind.FILE,
                        file_id=request.work_unit.file_id,
                    ),
                    evidence_refs=(
                        model_evidence.evidence_id,
                        changed_code_evidence.evidence_id,
                    ),
                    impact=str(raw["impact"]),
                    suggestion=str(raw["suggestion"]),
                    change_causation=str(raw["change_causation"]),
                    limitations=str(raw["limitations"]),
                    schema_version=_CANDIDATE_SCHEMA_VERSION,
                )
            )

        return tuple(candidates), tuple(evidence_items), ResponseState.ACCEPTED
