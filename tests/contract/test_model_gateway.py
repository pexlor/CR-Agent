from __future__ import annotations

from collections.abc import Mapping

import pytest

from code_review_agent.adapters.model.gateway import ModelGateway
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.execution.models import (
    ModelCallState,
    ModelCapabilities,
    ModelRequestOptions,
    ModelUsage,
    PromptEnvelope,
    ProviderSendResult,
    ProviderState,
    ReservationAction,
    ResponseState,
    StructuredOutputStrategy,
    UsageState,
)
from code_review_agent.ports.model import ModelGatewayPort
from tests.fakes.model_provider import FakeModelProvider


def capabilities(
    *,
    strategies: tuple[StructuredOutputStrategy, ...] = tuple(StructuredOutputStrategy),
) -> ModelCapabilities:
    return ModelCapabilities(
        provider_id="fake",
        provider_version="1",
        model_id="fake-model",
        origin="https://model.example.test",
        context_token_limit=10_000,
        max_output_tokens=1_000,
        structured_output_strategies=strategies,
        preflight_token_counting=True,
        usage_mapping_trusted=True,
        streaming_disabled=True,
        retries_disabled=True,
        dynamic_tools_disabled=True,
    )


def envelope() -> PromptEnvelope:
    return PromptEnvelope(
        system_rules="Review only the supplied change.",
        work_unit="unit-1",
        diff="@@ -1 +1 @@\n-old\n+new",
        tool_facts=("static:clean",),
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["findings"],
            "properties": {"findings": {"type": "array"}},
        },
    )


def options(
    strategy: StructuredOutputStrategy,
    **overrides: object,
) -> ModelRequestOptions:
    values: dict[str, object] = {
        "strategy": strategy,
        "max_output_tokens": 200,
    }
    values.update(overrides)
    return ModelRequestOptions(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("strategy", tuple(StructuredOutputStrategy))
def test_fake_provider_implements_shared_contract_for_fixed_strategy(
    strategy: StructuredOutputStrategy,
) -> None:
    provider: ModelGatewayPort = FakeModelProvider(capabilities())

    prepared = ModelGateway().prepare(provider, envelope(), options(strategy))

    assert prepared.strategy is strategy
    assert prepared.output_token_max == 200
    assert prepared.body_digest
    assert provider.capabilities.model_id == "fake-model"


@pytest.mark.parametrize(
    ("override", "value"),
    (
        ("streaming", True),
        ("automatic_retries", 1),
        ("fallback_model_id", "other-model"),
        ("dynamic_tools", ("repository_search",)),
    ),
)
def test_prohibited_call_features_are_rejected_before_provider_prepare(
    override: str, value: object
) -> None:
    provider = FakeModelProvider(capabilities())

    with pytest.raises(StableError) as caught:
        ModelGateway().prepare(
            provider,
            envelope(),
            options(StructuredOutputStrategy.JSON_SCHEMA, **{override: value}),
        )

    assert caught.value.code == "model_capability_mismatch"
    assert provider.prepare_calls == 0


def test_selected_strategy_does_not_fallback_when_unsupported() -> None:
    provider = FakeModelProvider(
        capabilities(strategies=(StructuredOutputStrategy.JSON_TEXT,))
    )

    with pytest.raises(StableError) as caught:
        ModelGateway().prepare(
            provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
        )

    assert caught.value.code == "model_capability_mismatch"
    assert provider.prepare_calls == 0


def test_incompatible_provider_safety_capability_is_rejected() -> None:
    unsafe = capabilities()
    unsafe = ModelCapabilities(
        provider_id=unsafe.provider_id,
        provider_version=unsafe.provider_version,
        model_id=unsafe.model_id,
        origin=unsafe.origin,
        context_token_limit=unsafe.context_token_limit,
        max_output_tokens=unsafe.max_output_tokens,
        structured_output_strategies=unsafe.structured_output_strategies,
        preflight_token_counting=unsafe.preflight_token_counting,
        usage_mapping_trusted=unsafe.usage_mapping_trusted,
        streaming_disabled=False,
        retries_disabled=True,
        dynamic_tools_disabled=True,
    )
    provider = FakeModelProvider(unsafe)

    with pytest.raises(StableError, match="model_capability_mismatch"):
        ModelGateway().prepare(
            provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
        )


@pytest.mark.asyncio
async def test_failure_before_request_is_sent_releases_reservation() -> None:
    provider = FakeModelProvider(
        capabilities(),
        (
            ProviderSendResult(
                provider_state=ProviderState.FAILED_KNOWN,
                request_sent=False,
                error_code="connection_setup_failed",
            ),
        ),
    )
    prepared = ModelGateway().prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await ModelGateway().send(provider, prepared, reservation_tokens=500)

    assert outcome.state == ModelCallState(
        ProviderState.FAILED_KNOWN, ResponseState.NOT_AVAILABLE
    )
    assert outcome.reservation_action is ReservationAction.RELEASE
    assert outcome.accounted_tokens == 0


@pytest.mark.asyncio
async def test_unknown_after_send_is_conservatively_committed_without_retry() -> None:
    provider = FakeModelProvider(
        capabilities(),
        (ProviderSendResult(provider_state=ProviderState.UNKNOWN),),
    )
    prepared = ModelGateway().prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await ModelGateway().send(provider, prepared, reservation_tokens=500)

    assert outcome.state == ModelCallState(
        ProviderState.UNKNOWN, ResponseState.NOT_AVAILABLE
    )
    assert outcome.reservation_action is ReservationAction.SETTLE_UNCERTAIN
    assert outcome.usage.state is UsageState.MISSING
    assert outcome.accounted_tokens == 500
    assert provider.send_calls == 1


@pytest.mark.asyncio
async def test_unknown_cannot_be_downgraded_by_reported_usage() -> None:
    provider = FakeModelProvider(
        capabilities(),
        (
            ProviderSendResult(
                provider_state=ProviderState.UNKNOWN,
                usage=ModelUsage(UsageState.KNOWN, 100, 50),
            ),
        ),
    )
    prepared = ModelGateway().prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await ModelGateway().send(provider, prepared, reservation_tokens=500)

    assert outcome.reservation_action is ReservationAction.SETTLE_UNCERTAIN
    assert outcome.accounted_tokens == 500
    assert outcome.usage.state is UsageState.UNTRUSTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "action", "accounted"),
    (
        (ModelUsage(UsageState.MISSING), ReservationAction.SETTLE_UNCERTAIN, 500),
        (
            ModelUsage(UsageState.UNTRUSTED, 300, 250),
            ReservationAction.SETTLE_UNCERTAIN,
            550,
        ),
        (
            ModelUsage(UsageState.UNTRUSTED, 100, 50),
            ReservationAction.SETTLE_UNCERTAIN,
            500,
        ),
    ),
)
async def test_missing_or_untrusted_usage_is_accounted_conservatively(
    usage: ModelUsage, action: ReservationAction, accounted: int
) -> None:
    provider = successful_provider(usage)
    prepared = ModelGateway().prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await ModelGateway().send(provider, prepared, reservation_tokens=500)

    assert outcome.state == ModelCallState(
        ProviderState.SUCCEEDED, ResponseState.PENDING
    )
    assert outcome.reservation_action is action
    assert outcome.accounted_tokens == accounted
    assert outcome.overage_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "accounted", "overage"),
    (
        (ModelUsage(UsageState.KNOWN, 100, 50), 150, 0),
        (ModelUsage(UsageState.KNOWN, 300, 250), 550, 50),
    ),
)
async def test_trusted_actual_usage_can_be_below_or_above_reservation(
    usage: ModelUsage, accounted: int, overage: int
) -> None:
    provider = successful_provider(usage)
    prepared = ModelGateway().prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await ModelGateway().send(provider, prepared, reservation_tokens=500)

    assert outcome.reservation_action is ReservationAction.SETTLE_KNOWN
    assert outcome.accounted_tokens == accounted
    assert outcome.overage_tokens == overage


@pytest.mark.asyncio
async def test_unexpected_provider_exception_becomes_unknown_without_error_text() -> (
    None
):
    class ExplodingProvider(FakeModelProvider):
        async def send_prepared(self, request: object) -> ProviderSendResult:
            self.send_calls += 1
            raise RuntimeError("credential=top-secret")

    provider = ExplodingProvider(capabilities())
    prepared = ModelGateway().prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await ModelGateway().send(provider, prepared, reservation_tokens=500)

    assert outcome.state.provider_state is ProviderState.UNKNOWN
    assert outcome.error_code == "model_result_unknown"
    assert "secret" not in repr(outcome)


def successful_provider(usage: ModelUsage) -> FakeModelProvider:
    payload: Mapping[str, object] = {"findings": []}
    return FakeModelProvider(
        capabilities(),
        (
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload=payload,
                usage=usage,
            ),
        ),
    )
