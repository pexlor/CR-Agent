"""Deterministic tests for the work unit execution pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

from code_review_agent.domain.budget.models import (
    ProviderHardBudgetCapability,
)
from code_review_agent.domain.budget.service import BudgetService
from code_review_agent.domain.common.digests import sha256_bytes
from code_review_agent.domain.execution.execution_models import (
    CoverageImpact,
    ModelAttemptOutcomeKind,
    WorkUnitExecutionState,
)
from code_review_agent.domain.execution.executor import (
    ExecutionRequest,
    WorkUnitExecutor,
)
from code_review_agent.domain.execution.models import (
    ModelCapabilities,
    ModelUsage,
    ProviderSendResult,
    ProviderState,
    StructuredOutputStrategy,
)
from code_review_agent.domain.execution.models import (
    UsageState as ModelUsageState,
)
from code_review_agent.domain.input.models import ArtifactSliceRef
from code_review_agent.domain.planning.models import (
    CapacityEstimate,
    LineRange,
    ModelCapacitySummary,
    PlanningStrategy,
    ToolFailureImpact,
    ToolSelection,
    WorkUnit,
    WorkUnitKind,
)
from code_review_agent.domain.security.models import (
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    SanitizedArtifactRef,
    SecurityDecision,
    SensitiveCategory,
)
from code_review_agent.domain.security.policy import SecurityPolicy
from code_review_agent.domain.security.service import SecurityService
from code_review_agent.ports.model import ModelGatewayPort
from code_review_agent.ports.tools import (
    FixedToolReference,
    ToolCatalogPort,
    ToolDeclaration,
    ToolExecutionResult,
    ToolExecutionState,
    ToolRegistrySnapshot,
)
from tests.fakes.model_provider import FakeModelProvider


def _artifact_ref(payload: str = "safe content") -> SanitizedArtifactRef:
    digest = sha256_bytes(payload.encode("utf-8"))
    return SanitizedArtifactRef(
        attestation_id="attestation-1",
        artifact_id="artifact-1",
        task_id="task-1",
        source=ArtifactSource.USER_CLI,
        kind=ArtifactKind.DIFF,
        purpose=ArtifactPurpose.DOMAIN_INGRESS,
        decision=SecurityDecision.SAFE,
        sanitized_digest=digest,
        policy_id="policy-1",
        policy_version=1,
        policy_digest="d" * 64,
    )


def _content_ref(payload: str = "safe content") -> ArtifactSliceRef:
    return ArtifactSliceRef(
        artifact=_artifact_ref(payload),
        start=0,
        end=len(payload),
        content_digest=sha256_bytes(payload.encode("utf-8")),
    )


def _work_unit(*, tools: tuple[ToolSelection, ...] = ()) -> WorkUnit:
    return WorkUnit(
        work_unit_id="work_unit_1",
        plan_id="plan_1",
        kind=WorkUnitKind.FILE,
        file_id="file_1",
        scope_ids=("scope_1",),
        range=LineRange(old_start=None, old_end=None, new_start=1, new_end=1),
        content_refs=(_content_ref(),),
        context_request=None,
        tools=tools,
        capacity_estimate=CapacityEstimate(
            estimator_version="v1",
            estimated_input_tokens=100,
            estimated_output_tokens=10,
        ),
        strategy=PlanningStrategy("file_first_v1", "1"),
        model_capacity=ModelCapacitySummary(
            provider_id="fake",
            provider_version="1",
            model_id="fake-model",
            context_token_limit=10_000,
            max_output_tokens=1_000,
            token_counting_version="v1",
        ),
        execution_rank=0,
    )


def _model_capabilities() -> ModelCapabilities:
    return ModelCapabilities(
        provider_id="fake",
        provider_version="1",
        model_id="fake-model",
        origin="https://model.example.test",
        context_token_limit=10_000,
        max_output_tokens=1_000,
        structured_output_strategies=(StructuredOutputStrategy.JSON_SCHEMA,),
        preflight_token_counting=True,
        usage_mapping_trusted=True,
        streaming_disabled=True,
        retries_disabled=True,
        dynamic_tools_disabled=True,
        request_method="POST",
        request_path="/model",
        fixed_headers={
            "accept": "application/json",
            "content-type": "application/json",
        },
    )


def _capability(digest: str) -> ProviderHardBudgetCapability:
    return ProviderHardBudgetCapability(
        capability_id="capability-1",
        provider_id="fake",
        provider_version="1",
        model_id="fake-model",
        origin="https://model.example.test",
        prepared_request_digest=digest,
        token_counted_request_digest=digest,
        max_output_enforced=True,
        usage_mapping_trusted=True,
        retries_disabled=True,
    )


class _NoOpScanner:
    def scan(self, content, descriptor, policy):  # type: ignore[no-untyped-def]
        from code_review_agent.domain.security.models import ScanResult

        return ScanResult.complete((), policy.detector_manifest)


def _security_service() -> SecurityService:
    policy = SecurityPolicy(
        policy_id="policy-1",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset(),
        detector_manifest=("noop@1",),
        action_matrix={},
        purpose_transitions=frozenset(
            {
                (ArtifactPurpose.DOMAIN_INGRESS, ArtifactPurpose.TRACE_MODEL_REQUEST),
                (ArtifactPurpose.MODEL_EGRESS, ArtifactPurpose.TRACE_MODEL_RESPONSE),
            }
        ),
    )
    return SecurityService(_NoOpScanner(), policy=policy)


def test_model_envelope_requires_array_only_review_output() -> None:
    provider = FakeModelProvider(_model_capabilities(), results=())
    budget_service, account_id = _budget()
    request = _request(
        provider=provider,
        budget_service=budget_service,
        account_id=account_id,
    )

    envelope = WorkUnitExecutor()._build_envelope(request, ())

    assert '"findings" must always be an array' in envelope.system_rules
    assert "Do not return summary" in envelope.system_rules


def test_model_envelope_requires_chinese_document_content() -> None:
    provider = FakeModelProvider(_model_capabilities(), results=())
    budget_service, account_id = _budget()
    request = _request(
        provider=provider,
        budget_service=budget_service,
        account_id=account_id,
    )

    envelope = WorkUnitExecutor()._build_envelope(request, ())

    assert "Simplified Chinese" in envelope.system_rules
    assert "JSON property names" in envelope.system_rules


class _NoToolCatalog:
    def freeze(self) -> ToolRegistrySnapshot:
        raise AssertionError("no tool catalog should be frozen in this test")

    def catalog(self) -> tuple[ToolDeclaration, ...]:
        return ()

    def resolve_exact(self, tool_id: str, version: str) -> ToolDeclaration:
        raise AssertionError("no tool should be resolved in this test")

    def verify_fixed_reference(self, reference: FixedToolReference) -> ToolDeclaration:
        raise AssertionError("no tool should be resolved in this test")

    def disable_version(self, reference: FixedToolReference, *, reason: str) -> None:
        raise AssertionError("no tool should be disabled in this test")


def _budget(*, tokens: int = 10_000) -> tuple[BudgetService, str]:
    service = BudgetService()
    account = service.create_account("task-1", tokens, capability_ref="capability-1")
    return service, account.account_id


def _request(
    *,
    provider: ModelGatewayPort,
    budget_service: BudgetService,
    account_id: str,
    work_unit: WorkUnit | None = None,
    tool_catalog: ToolCatalogPort | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        task_id="task-1",
        work_unit=work_unit or _work_unit(),
        attempt_number=1,
        model_provider=provider,
        model_options_strategy=StructuredOutputStrategy.JSON_SCHEMA,
        max_output_tokens=200,
        budget_service=budget_service,
        budget_account_id=account_id,
        security_service=_security_service(),
        tool_catalog=tool_catalog or _NoToolCatalog(),
        system_rules="Review only the supplied change.",
        diff_text="@@ -1 +1 @@\n-old\n+new",
    )


def test_successful_execution_produces_one_model_attempt_and_candidates() -> None:
    provider = FakeModelProvider(
        _model_capabilities(),
        results=(
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={"findings": []},
                usage=ModelUsage(
                    ModelUsageState.KNOWN, input_tokens=5, output_tokens=3
                ),
            ),
        ),
    )
    budget_service, account_id = _budget()

    result = asyncio.run(
        WorkUnitExecutor().execute(
            _request(
                provider=provider, budget_service=budget_service, account_id=account_id
            )
        )
    )

    assert result.state is WorkUnitExecutionState.SUCCEEDED
    assert result.model_outcome_kind is ModelAttemptOutcomeKind.SUCCEEDED
    assert result.model_attempt is not None
    assert provider.send_calls == 1
    assert result.coverage_impact is CoverageImpact.FULLY_COVERED


def test_successful_finding_retains_model_and_changed_code_evidence() -> None:
    provider = FakeModelProvider(
        _model_capabilities(),
        results=(
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={
                    "findings": [
                        {
                            "category": "correctness",
                            "title": "Unchecked result",
                            "problem": "The result is ignored.",
                            "trigger_condition": "When the call fails.",
                            "impact": "The operation can silently fail.",
                            "suggestion": "Handle the returned error.",
                            "change_causation": "The changed code drops the result.",
                            "limitations": "",
                        }
                    ]
                },
                usage=ModelUsage(
                    ModelUsageState.KNOWN, input_tokens=5, output_tokens=3
                ),
            ),
        ),
    )
    budget_service, account_id = _budget()

    result = asyncio.run(
        WorkUnitExecutor().execute(
            _request(
                provider=provider, budget_service=budget_service, account_id=account_id
            )
        )
    )

    assert len(result.candidates) == 1
    evidence_types = {item.evidence_type for item in result.evidence}
    assert evidence_types == {"changed_code", "model_response"}
    assert len(result.candidates[0].evidence_refs) == 2


def test_at_most_one_model_call_per_execution() -> None:
    provider = FakeModelProvider(
        _model_capabilities(),
        results=(
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={"findings": []},
                usage=ModelUsage(
                    ModelUsageState.KNOWN, input_tokens=5, output_tokens=3
                ),
            ),
        ),
    )
    budget_service, account_id = _budget()

    asyncio.run(
        WorkUnitExecutor().execute(
            _request(
                provider=provider, budget_service=budget_service, account_id=account_id
            )
        )
    )

    assert provider.send_calls == 1
    assert provider.prepare_calls == 1


def test_explicit_retry_requires_a_new_execution_with_new_attempt_number() -> None:
    provider = FakeModelProvider(
        _model_capabilities(),
        results=(
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={"findings": []},
                usage=ModelUsage(
                    ModelUsageState.KNOWN, input_tokens=5, output_tokens=3
                ),
            ),
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={"findings": []},
                usage=ModelUsage(
                    ModelUsageState.KNOWN, input_tokens=5, output_tokens=3
                ),
            ),
        ),
    )
    budget_service, account_id = _budget()

    first = asyncio.run(
        WorkUnitExecutor().execute(
            _request(
                provider=provider, budget_service=budget_service, account_id=account_id
            )
        )
    )
    second_request = _request(
        provider=provider, budget_service=budget_service, account_id=account_id
    )
    second_request = replace(second_request, attempt_number=2)
    second = asyncio.run(WorkUnitExecutor().execute(second_request))

    assert first.execution_id != second.execution_id
    assert first.attempt_number == 1
    assert second.attempt_number == 2
    assert provider.send_calls == 2


def test_unknown_provider_result_marks_execution_unknown() -> None:
    provider = FakeModelProvider(
        _model_capabilities(),
        results=(
            ProviderSendResult(
                provider_state=ProviderState.UNKNOWN,
                request_sent=True,
            ),
        ),
    )
    budget_service, account_id = _budget()

    result = asyncio.run(
        WorkUnitExecutor().execute(
            _request(
                provider=provider, budget_service=budget_service, account_id=account_id
            )
        )
    )

    assert result.state is WorkUnitExecutionState.UNKNOWN
    assert result.model_outcome_kind is ModelAttemptOutcomeKind.UNKNOWN
    assert result.candidates == ()


def test_security_rejected_response_settles_usage_but_produces_no_candidates() -> None:
    provider = FakeModelProvider(
        _model_capabilities(),
        results=(
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={
                    "findings": [
                        {
                            "category": "security",
                            "title": "leak",
                            "problem": "contains SECRET_MARKER token",
                            "trigger_condition": "always",
                            "impact": "leak",
                            "suggestion": "remove",
                            "change_causation": "introduced",
                            "limitations": "none",
                        }
                    ]
                },
                usage=ModelUsage(
                    ModelUsageState.KNOWN, input_tokens=5, output_tokens=3
                ),
            ),
        ),
    )
    budget_service, account_id = _budget()

    class _MarkerScanner:
        def scan(self, content, descriptor, policy):  # type: ignore[no-untyped-def]
            from code_review_agent.domain.security.models import (
                ScanResult,
                SecurityFinding,
                SensitiveCategory,
            )

            marker = "SECRET_MARKER"
            if descriptor.purpose is not ArtifactPurpose.DOMAIN_INGRESS or (
                marker not in content
            ):
                return ScanResult.complete((), policy.detector_manifest)
            start = content.index(marker)
            return ScanResult.complete(
                (
                    SecurityFinding(
                        detector_id="fixture",
                        category=SensitiveCategory.CREDENTIAL,
                        start=start,
                        end=start + len(marker),
                        confidence="high",
                        rule_id="rule-1",
                        crosses_boundary=False,
                        action="block",
                        summary="blocked response",
                    ),
                ),
                policy.detector_manifest,
            )

    policy = SecurityPolicy(
        policy_id="policy-1",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset(
            {
                __import__(
                    "code_review_agent.domain.security.models",
                    fromlist=["SensitiveCategory"],
                ).SensitiveCategory.CREDENTIAL
            }
        ),
        detector_manifest=("fixture@1",),
        action_matrix={
            __import__(
                "code_review_agent.domain.security.models",
                fromlist=["SensitiveCategory"],
            ).SensitiveCategory.CREDENTIAL: SecurityDecision.BLOCKED
        },
        purpose_transitions=frozenset(
            {
                (ArtifactPurpose.DOMAIN_INGRESS, ArtifactPurpose.TRACE_MODEL_REQUEST),
                (ArtifactPurpose.MODEL_EGRESS, ArtifactPurpose.TRACE_MODEL_RESPONSE),
            }
        ),
    )
    security_service = SecurityService(_MarkerScanner(), policy=policy)
    request = _request(
        provider=provider, budget_service=budget_service, account_id=account_id
    )
    request = replace(request, security_service=security_service)

    result = asyncio.run(WorkUnitExecutor().execute(request))

    assert result.model_outcome_kind is ModelAttemptOutcomeKind.SUCCEEDED
    assert result.candidates == ()
    assert result.coverage_impact is CoverageImpact.DEGRADED
    summary = budget_service.get_summary(account_id)
    assert summary.known_consumption == 8


def test_optional_tool_failure_degrades_evidence_but_continues() -> None:
    class _StubCatalog:
        def freeze(self) -> ToolRegistrySnapshot:
            raise AssertionError("not needed in this test")

        def catalog(self) -> tuple[ToolDeclaration, ...]:
            return ()

        def resolve_exact(self, tool_id: str, version: str) -> ToolDeclaration:
            raise AssertionError("not needed in this test")

        def verify_fixed_reference(
            self, reference: FixedToolReference
        ) -> ToolDeclaration:
            return cast(
                ToolDeclaration,
                SimpleNamespace(tool_id=reference.tool_id, version=reference.version),
            )

        def disable_version(
            self, reference: FixedToolReference, *, reason: str
        ) -> None:
            raise AssertionError("not needed in this test")

    class _FailingRuntime:
        def execute(self, fixed_tool_ref, authorized_input, planned_limits, context):  # type: ignore[no-untyped-def]
            return ToolExecutionResult(
                state=ToolExecutionState.FAILED_KNOWN,
                tool_id=fixed_tool_ref.tool_id,
                tool_version=fixed_tool_ref.version,
                interpreter_version="1.0.0",
                input_digest=authorized_input.input_digest,
                limits_digest="a" * 64,
                steps=1,
                evidence=(),
                result_digest="b" * 64,
                error_code="tool_max_steps_exceeded",
            )

    tool_ref = FixedToolReference(
        tool_id="scanner",
        version="1.0.0",
        contract_version="1.0",
        origin="builtin",
        resource_digest="a" * 64,
        schema_digest="b" * 64,
        declaration_digest="c" * 64,
    )
    from code_review_agent.domain.planning.models import ToolSelection
    from code_review_agent.ports.tools import ToolLimits

    tool_selection = ToolSelection(
        fixed_reference=tool_ref,
        applicable_rule_ids=("rule-1",),
        applicability_reason="matches file language",
        planned_limits=ToolLimits(
            max_input_bytes=1024,
            max_tokens=256,
            max_rules=10,
            max_steps=100,
            max_matches=10,
            max_output_items=10,
            max_field_length=64,
            soft_time_ms=1000,
        ),
        failure_impact=ToolFailureImpact.EVIDENCE_DEGRADED,
        order=0,
    )
    provider = FakeModelProvider(
        _model_capabilities(),
        results=(
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={"findings": []},
                usage=ModelUsage(
                    ModelUsageState.KNOWN, input_tokens=5, output_tokens=3
                ),
            ),
        ),
    )
    budget_service, account_id = _budget()
    request = _request(
        provider=provider,
        budget_service=budget_service,
        account_id=account_id,
        work_unit=_work_unit(tools=(tool_selection,)),
        tool_catalog=_StubCatalog(),
    )
    request = replace(request, tool_runtime=_FailingRuntime())

    result = asyncio.run(WorkUnitExecutor().execute(request))

    assert result.state is WorkUnitExecutionState.SUCCEEDED
    assert len(result.tool_attempts) == 1
    assert result.tool_attempts[0].error_code == "tool_max_steps_exceeded"
    assert result.coverage_impact is CoverageImpact.DEGRADED


def test_coverage_degrading_tool_failure_makes_execution_partial() -> None:
    class _StubCatalog:
        def freeze(self) -> ToolRegistrySnapshot:
            raise AssertionError("not needed in this test")

        def catalog(self) -> tuple[ToolDeclaration, ...]:
            return ()

        def resolve_exact(self, tool_id: str, version: str) -> ToolDeclaration:
            raise AssertionError("not needed in this test")

        def verify_fixed_reference(
            self, reference: FixedToolReference
        ) -> ToolDeclaration:
            return cast(
                ToolDeclaration,
                SimpleNamespace(tool_id=reference.tool_id, version=reference.version),
            )

        def disable_version(
            self, reference: FixedToolReference, *, reason: str
        ) -> None:
            raise AssertionError("not needed in this test")

    class _FailingRuntime:
        def execute(self, fixed_tool_ref, authorized_input, planned_limits, context):  # type: ignore[no-untyped-def]
            return ToolExecutionResult(
                state=ToolExecutionState.FAILED_KNOWN,
                tool_id=fixed_tool_ref.tool_id,
                tool_version=fixed_tool_ref.version,
                interpreter_version="1.0.0",
                input_digest=authorized_input.input_digest,
                limits_digest="a" * 64,
                steps=1,
                evidence=(),
                result_digest="b" * 64,
                error_code="tool_max_steps_exceeded",
            )

    tool_ref = FixedToolReference(
        tool_id="scanner",
        version="1.0.0",
        contract_version="1.0",
        origin="builtin",
        resource_digest="a" * 64,
        schema_digest="b" * 64,
        declaration_digest="c" * 64,
    )
    from code_review_agent.domain.planning.models import ToolSelection
    from code_review_agent.ports.tools import ToolLimits

    tool_selection = ToolSelection(
        fixed_reference=tool_ref,
        applicable_rule_ids=("rule-1",),
        applicability_reason="matches file language",
        planned_limits=ToolLimits(
            max_input_bytes=1024,
            max_tokens=256,
            max_rules=10,
            max_steps=100,
            max_matches=10,
            max_output_items=10,
            max_field_length=64,
            soft_time_ms=1000,
        ),
        failure_impact=ToolFailureImpact.COVERAGE_DEGRADED,
        order=0,
    )
    provider = FakeModelProvider(_model_capabilities())
    budget_service, account_id = _budget()
    request = _request(
        provider=provider,
        budget_service=budget_service,
        account_id=account_id,
        work_unit=_work_unit(tools=(tool_selection,)),
        tool_catalog=_StubCatalog(),
    )
    request = replace(request, tool_runtime=_FailingRuntime())

    result = asyncio.run(WorkUnitExecutor().execute(request))

    assert result.state is WorkUnitExecutionState.PARTIAL
    assert result.coverage_impact is CoverageImpact.NOT_COVERED
    assert provider.send_calls == 0


def test_insufficient_budget_prevents_a_model_call() -> None:
    provider = FakeModelProvider(_model_capabilities())
    budget_service, account_id = _budget(tokens=1)

    result = asyncio.run(
        WorkUnitExecutor().execute(
            _request(
                provider=provider, budget_service=budget_service, account_id=account_id
            )
        )
    )

    assert result.state is WorkUnitExecutionState.FAILED
    assert provider.send_calls == 0
    assert result.model_outcome_kind is ModelAttemptOutcomeKind.NOT_ATTEMPTED


def test_candidate_finding_only_references_its_own_execution_material() -> None:
    provider = FakeModelProvider(
        _model_capabilities(),
        results=(
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={
                    "findings": [
                        {
                            "category": "security",
                            "title": "insecure default",
                            "problem": "uses eval",
                            "trigger_condition": "always",
                            "impact": "code execution",
                            "suggestion": "avoid eval",
                            "change_causation": "introduced in this diff",
                            "limitations": "none",
                        }
                    ]
                },
                usage=ModelUsage(
                    ModelUsageState.KNOWN, input_tokens=5, output_tokens=3
                ),
            ),
        ),
    )
    budget_service, account_id = _budget()

    result = asyncio.run(
        WorkUnitExecutor().execute(
            _request(
                provider=provider, budget_service=budget_service, account_id=account_id
            )
        )
    )

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.execution_id == result.execution_id
    assert candidate.work_unit_id == result.work_unit_id
    assert candidate.task_id == result.task_id
    assert len(result.evidence) == 2
    assert set(candidate.evidence_refs) == {
        item.evidence_id for item in result.evidence
    }


def test_outbound_model_request_is_scanned_before_being_sent() -> None:
    class _BlockingOutboundScanner:
        def scan(self, content, descriptor, policy):  # type: ignore[no-untyped-def]
            from code_review_agent.domain.security.models import (
                ScanResult,
                SecurityFinding,
                SensitiveCategory,
            )

            if descriptor.purpose is not ArtifactPurpose.MODEL_EGRESS:
                return ScanResult.complete((), policy.detector_manifest)
            return ScanResult.complete(
                (
                    SecurityFinding(
                        detector_id="fixture",
                        category=SensitiveCategory.CREDENTIAL,
                        start=0,
                        end=1,
                        confidence="high",
                        rule_id="rule-1",
                        crosses_boundary=False,
                        action="block",
                        summary="blocked request",
                    ),
                ),
                policy.detector_manifest,
            )

    policy = SecurityPolicy(
        policy_id="policy-1",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset({SensitiveCategory.CREDENTIAL}),
        detector_manifest=("fixture@1",),
        action_matrix={SensitiveCategory.CREDENTIAL: SecurityDecision.BLOCKED},
        purpose_transitions=frozenset(
            {
                (ArtifactPurpose.DOMAIN_INGRESS, ArtifactPurpose.TRACE_MODEL_REQUEST),
                (ArtifactPurpose.MODEL_EGRESS, ArtifactPurpose.TRACE_MODEL_RESPONSE),
            }
        ),
    )
    security_service = SecurityService(_BlockingOutboundScanner(), policy=policy)
    provider = FakeModelProvider(_model_capabilities())
    budget_service, account_id = _budget()
    request = _request(
        provider=provider, budget_service=budget_service, account_id=account_id
    )
    request = replace(request, security_service=security_service)

    result = asyncio.run(WorkUnitExecutor().execute(request))

    assert result.state is WorkUnitExecutionState.FAILED
    assert result.error_code == "security_boundary_failed"
    assert provider.send_calls == 0
