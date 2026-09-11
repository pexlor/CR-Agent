from collections.abc import Mapping

import pytest

from code_review_agent.domain.common.digests import canonical_json
from code_review_agent.domain.execution.models import (
    ModelCallState,
    ModelUsage,
    PromptEnvelope,
    ProviderState,
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
