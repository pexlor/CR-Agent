from collections.abc import Mapping

import pytest

from code_review_agent.domain.common.digests import canonical_json
from code_review_agent.domain.execution.models import (
    ModelCallOutcome,
    ModelCallState,
    ModelUsage,
    PromptEnvelope,
    ProviderSendResult,
    ProviderState,
    ReservationAction,
    ResponseState,
    StructuredOutputStrategy,
    UsageState,
)


@pytest.mark.parametrize(
    "provider_state",
    (
        ProviderState.PENDING,
        ProviderState.RESERVED,
        ProviderState.RUNNING,
        ProviderState.FAILED_KNOWN,
        ProviderState.UNKNOWN,
    ),
)
def test_response_is_unavailable_without_provider_success(
    provider_state: ProviderState,
) -> None:
    state = ModelCallState(provider_state, ResponseState.NOT_AVAILABLE)

    assert state.provider_state is provider_state
    assert state.response_state is ResponseState.NOT_AVAILABLE


@pytest.mark.parametrize(
    "response_state",
    (
        ResponseState.PENDING,
        ResponseState.ACCEPTED,
        ResponseState.INVALID_OUTPUT,
        ResponseState.SECURITY_REJECTED,
    ),
)
def test_provider_success_preserves_independent_response_state(
    response_state: ResponseState,
) -> None:
    state = ModelCallState(ProviderState.SUCCEEDED, response_state)

    assert state.provider_state is ProviderState.SUCCEEDED
    assert state.response_state is response_state


def test_invalid_axis_combinations_are_rejected() -> None:
    with pytest.raises(ValueError, match="response state"):
        ModelCallState(ProviderState.RUNNING, ResponseState.ACCEPTED)
    with pytest.raises(ValueError, match="provider success"):
        ModelCallState(ProviderState.SUCCEEDED, ResponseState.NOT_AVAILABLE)


def test_state_and_strategy_values_are_stable() -> None:
    assert {state.value for state in ProviderState} == {
        "pending",
        "reserved",
        "running",
        "succeeded",
        "failed_known",
        "unknown",
    }
    assert {state.value for state in ResponseState} == {
        "not_available",
        "pending",
        "accepted",
        "invalid_output",
        "security_rejected",
    }
    assert {strategy.value for strategy in StructuredOutputStrategy} == {
        "json_schema",
        "tool_calling",
        "json_text",
    }


def test_usage_requires_counts_only_when_known() -> None:
    usage = ModelUsage(UsageState.KNOWN, input_tokens=8, output_tokens=5)
    assert usage.reported_total == 13

    missing = ModelUsage(UsageState.MISSING)
    assert missing.reported_total is None

    untrusted = ModelUsage(UsageState.UNTRUSTED, input_tokens=10, output_tokens=7)
    assert untrusted.reported_total == 17

    with pytest.raises(ValueError, match="known usage"):
        ModelUsage(UsageState.KNOWN)
    with pytest.raises(ValueError, match="missing usage"):
        ModelUsage(UsageState.MISSING, input_tokens=1, output_tokens=2)


def test_prompt_envelope_is_deeply_immutable_and_complete() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"findings": {"type": "array", "items": []}},
    }
    context = ["src/example.py:1"]
    prohibited = ["streaming", "dynamic_tools"]
    value = PromptEnvelope(
        system_rules="review",
        work_unit="unit-1",
        diff="diff",
        controlled_context=context,  # type: ignore[arg-type]
        tool_facts=("tool:fact",),
        prohibited_capabilities=prohibited,  # type: ignore[arg-type]
        version_digest="versions-1",
        output_schema=schema,
    )
    frozen_schema = canonical_json(value.output_schema)

    context.append("src/other.py:2")
    prohibited.append("fallback")
    properties = schema["properties"]
    assert isinstance(properties, dict)
    properties["extra"] = {"type": "string"}

    assert value.controlled_context == ("src/example.py:1",)
    assert value.prohibited_capabilities == ("streaming", "dynamic_tools")
    assert canonical_json(value.output_schema) == frozen_schema
    nested = value.output_schema["properties"]
    assert isinstance(nested, Mapping)
    with pytest.raises(TypeError):
        nested["extra"] = {}  # type: ignore[index]


def test_unsent_known_failure_usage_is_rejected_as_contradictory() -> None:
    with pytest.raises(ValueError, match="unsent"):
        ProviderSendResult(
            provider_state=ProviderState.FAILED_KNOWN,
            request_sent=False,
            usage=ModelUsage(UsageState.KNOWN, 1, 1),
        )


def test_provider_and_outcome_payloads_are_deeply_immutable() -> None:
    provider_payload: dict[str, object] = {
        "findings": [{"title": "original"}],
    }
    provider_result = ProviderSendResult(
        provider_state=ProviderState.SUCCEEDED,
        request_sent=True,
        response_payload=provider_payload,
        usage=ModelUsage(UsageState.KNOWN, 1, 1),
    )
    outcome_payload: dict[str, object] = {
        "findings": [{"title": "outcome"}],
    }
    outcome = ModelCallOutcome(
        state=ModelCallState(ProviderState.SUCCEEDED, ResponseState.PENDING),
        reservation_action=ReservationAction.SETTLE_KNOWN,
        usage=ModelUsage(UsageState.KNOWN, 1, 1),
        accounted_tokens=2,
        overage_tokens=0,
        response_payload=outcome_payload,
    )
    provider_snapshot = canonical_json(provider_result.response_payload)
    outcome_snapshot = canonical_json(outcome.response_payload)

    cast_provider_findings = provider_payload["findings"]
    assert isinstance(cast_provider_findings, list)
    cast_provider_findings.append({"title": "mutated"})
    cast_outcome_findings = outcome_payload["findings"]
    assert isinstance(cast_outcome_findings, list)
    cast_outcome_findings.append({"title": "mutated"})

    assert canonical_json(provider_result.response_payload) == provider_snapshot
    assert canonical_json(outcome.response_payload) == outcome_snapshot
    assert provider_result.response_payload is not None
    frozen_findings = provider_result.response_payload["findings"]
    assert isinstance(frozen_findings, tuple)
    nested_finding = frozen_findings[0]
    assert isinstance(nested_finding, Mapping)
    with pytest.raises(TypeError):
        nested_finding["title"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize(
    "payload",
    ({"findings": {"not-json"}}, {"score": float("nan")}),
)
def test_provider_payload_rejects_non_strict_json(
    payload: Mapping[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProviderSendResult(
            provider_state=ProviderState.SUCCEEDED,
            request_sent=True,
            response_payload=payload,
        )


def test_model_outcome_payload_rejects_non_strict_json() -> None:
    with pytest.raises(TypeError):
        ModelCallOutcome(
            state=ModelCallState(ProviderState.SUCCEEDED, ResponseState.PENDING),
            reservation_action=ReservationAction.SETTLE_KNOWN,
            usage=ModelUsage(UsageState.KNOWN, 1, 1),
            accounted_tokens=2,
            overage_tokens=0,
            response_payload={"findings": {"not-json"}},
        )
