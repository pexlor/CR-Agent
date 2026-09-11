from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

import pytest

from code_review_agent.adapters.model.gateway import ModelGateway
from code_review_agent.domain.budget.models import BudgetReservation, ReservationState
from code_review_agent.domain.common.digests import canonical_json
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.execution.models import (
    ModelCallState,
    ModelCapabilities,
    ModelRequestOptions,
    ModelUsage,
    PreparedModelRequest,
    PromptEnvelope,
    ProviderSendResult,
    ProviderState,
    ReservationAction,
    ResponseState,
    StructuredOutputStrategy,
    UsageState,
)
from code_review_agent.ports.model import ModelGatewayPort
from tests.contract.model_gateway_harness import (
    ProviderScenario,
    assert_provider_preparation_contract,
    assert_provider_send_contract,
)
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
        request_method="POST",
        request_path="/model",
        fixed_headers={
            "accept": "application/json",
            "content-type": "application/json",
        },
    )


def envelope() -> PromptEnvelope:
    return PromptEnvelope(
        system_rules="Review only the supplied change.",
        work_unit="unit-1",
        diff="@@ -1 +1 @@\n-old\n+new",
        controlled_context=("src/example.py:1",),
        tool_facts=("static:clean",),
        prohibited_capabilities=("dynamic_tools", "streaming"),
        version_digest="versions-1",
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


def reservation_for(prepared: PreparedModelRequest) -> BudgetReservation:
    amount = prepared.input_token_bound + prepared.output_token_max
    return BudgetReservation(
        reservation_id=f"reservation-{prepared.preparation_id}",
        account_id="budget-1",
        task_id="task-1",
        model_call_id=f"call-{prepared.preparation_id}",
        work_unit_id="unit-1",
        input_bound=prepared.input_token_bound,
        output_max=prepared.output_token_max,
        amount=amount,
        prepared_request_digest=prepared.body_digest,
        capability_ref="capability-1",
        state=ReservationState.ACTIVE,
    )


@pytest.mark.parametrize("strategy", tuple(StructuredOutputStrategy))
def test_fake_provider_implements_reusable_contract_for_fixed_strategy(
    strategy: StructuredOutputStrategy,
) -> None:
    provider: ModelGatewayPort = FakeModelProvider(capabilities())

    first, second = assert_provider_preparation_contract(
        provider, envelope(), options(strategy)
    )

    assert first.strategy is strategy
    assert second.strategy is strategy


@pytest.mark.parametrize("strategy", tuple(StructuredOutputStrategy))
def test_fake_provider_has_fixed_wire_semantics(
    strategy: StructuredOutputStrategy,
) -> None:
    provider = FakeModelProvider(capabilities())

    prepared = provider.prepare_request(envelope(), options(strategy))
    body = json.loads(prepared.body)
    expected_schema = json.loads(canonical_json(envelope().output_schema))

    assert body["controlled_context"] == ["src/example.py:1"]
    assert body["prohibited_capabilities"] == ["dynamic_tools", "streaming"]
    assert body["version_digest"] == "versions-1"
    if strategy is StructuredOutputStrategy.JSON_SCHEMA:
        assert body["structured_output"] == {
            "mode": "json_schema",
            "schema": expected_schema,
        }
    elif strategy is StructuredOutputStrategy.TOOL_CALLING:
        assert body["structured_output"] == {
            "mode": "tool_calling",
            "tool": {
                "input_schema": expected_schema,
                "name": "submit_review",
            },
        }
    else:
        assert body["structured_output"] == {
            "instruction": "return_json_only",
            "mode": "json_text",
            "schema": expected_schema,
        }


def test_fake_provider_counts_are_not_part_of_shared_contract() -> None:
    provider = FakeModelProvider(capabilities())

    provider.prepare_request(envelope(), options(StructuredOutputStrategy.JSON_SCHEMA))

    assert provider.prepare_calls == 1


@pytest.mark.asyncio
async def test_fake_provider_implements_reusable_send_contract() -> None:
    await assert_provider_send_contract(
        fake_provider_for_scenario,
        envelope(),
        options(StructuredOutputStrategy.JSON_SCHEMA),
    )


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
    unsafe = replace(capabilities(), streaming_disabled=False)
    provider = FakeModelProvider(unsafe)

    with pytest.raises(StableError, match="model_capability_mismatch"):
        ModelGateway().prepare(
            provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
        )


def test_provider_without_preflight_token_counting_is_rejected() -> None:
    provider = FakeModelProvider(
        replace(capabilities(), preflight_token_counting=False)
    )

    with pytest.raises(StableError, match="model_capability_mismatch"):
        ModelGateway().prepare(
            provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
        )

    assert provider.prepare_calls == 0


@pytest.mark.parametrize(
    "header_name",
    (
        "authorization",
        "cookie",
        "x-auth-token",
        "x-amz-security-token",
        "x-arbitrary-header",
    ),
)
def test_model_headers_use_an_explicit_non_authentication_allowlist(
    header_name: str,
) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        replace(capabilities(), fixed_headers={header_name: "secret-or-user-input"})


def test_fixed_protocol_header_is_allowed() -> None:
    value = replace(
        capabilities(),
        fixed_headers={
            "accept": "application/json",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    assert value.fixed_headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    "origin",
    (
        "http://model.example.test",
        "https://user:password@model.example.test",
        "https://model.example.test/v1",
        "https://model.example.test?tenant=user-input",
        "https://model.example.test#fragment",
        "https://model.example.test:8443",
        "https://model example.test",
        "https://",
    ),
)
def test_provider_origin_rejects_non_origin_or_unsafe_urls(origin: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        replace(capabilities(), origin=origin)


def test_provider_origin_allows_only_default_or_explicit_https_port() -> None:
    implicit = capabilities()
    explicit = replace(capabilities(), origin="https://model.example.test:443")

    assert implicit.origin == "https://model.example.test"
    assert explicit.origin == "https://model.example.test:443"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    tuple(
        (field, invalid_value)
        for field in (
            "preflight_token_counting",
            "usage_mapping_trusted",
            "streaming_disabled",
            "retries_disabled",
            "dynamic_tools_disabled",
        )
        for invalid_value in ("false", 1)
    ),
)
def test_model_capability_flags_require_real_booleans(
    field: str, invalid_value: object
) -> None:
    with pytest.raises(TypeError, match="bool"):
        capability_with_invalid_flag(field, invalid_value)


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
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await gateway.send(
        provider, prepared, reservation=reservation_for(prepared)
    )

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
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await gateway.send(
        provider, prepared, reservation=reservation_for(prepared)
    )

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
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await gateway.send(
        provider, prepared, reservation=reservation_for(prepared)
    )

    assert outcome.reservation_action is ReservationAction.SETTLE_UNCERTAIN
    assert outcome.accounted_tokens == 500
    assert outcome.usage.state is UsageState.UNTRUSTED


@pytest.mark.asyncio
async def test_untrusted_provider_usage_capability_downgrades_known_usage() -> None:
    provider = FakeModelProvider(
        replace(capabilities(), usage_mapping_trusted=False),
        (
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={"findings": []},
                usage=ModelUsage(UsageState.KNOWN, 300, 250),
            ),
        ),
    )
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await gateway.send(
        provider, prepared, reservation=reservation_for(prepared)
    )

    assert outcome.reservation_action is ReservationAction.SETTLE_UNCERTAIN
    assert outcome.usage.state is UsageState.UNTRUSTED
    assert outcome.accounted_tokens == 550
    assert outcome.overage_tokens == 0


@pytest.mark.asyncio
async def test_gateway_rejects_request_not_issued_by_its_prepare() -> None:
    provider = successful_provider(ModelUsage(UsageState.KNOWN, 100, 50))
    prepared = provider.prepare_request(
        envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    with pytest.raises(StableError) as caught:
        await ModelGateway().send(
            provider, prepared, reservation=reservation_for(prepared)
        )

    assert caught.value.code == "model_capability_mismatch"
    assert provider.send_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("method", "GET"),
        ("path", "https://attacker.example/model"),
        ("headers", {"content-type": "text/plain"}),
        ("strategy", StructuredOutputStrategy.JSON_TEXT),
        ("output_token_max", 1_001),
        ("preparation_id", "forged"),
    ),
)
async def test_gateway_revalidates_fixed_request_before_send(
    field: str, invalid_value: object
) -> None:
    provider = successful_provider(ModelUsage(UsageState.KNOWN, 100, 50))
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )
    object.__setattr__(prepared, field, invalid_value)

    with pytest.raises(StableError) as caught:
        await gateway.send(provider, prepared, reservation=reservation_for(prepared))

    assert caught.value.code == "model_capability_mismatch"
    assert provider.send_calls == 0


@pytest.mark.asyncio
async def test_gateway_recomputes_body_digest_before_send() -> None:
    provider = successful_provider(ModelUsage(UsageState.KNOWN, 100, 50))
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )
    object.__setattr__(prepared, "body", b'{"tampered":true}')

    with pytest.raises(StableError) as caught:
        await gateway.send(provider, prepared, reservation=reservation_for(prepared))

    assert caught.value.code == "model_capability_mismatch"
    assert provider.send_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ("digest", "input_bound", "output_max", "amount", "state"),
)
async def test_gateway_rejects_reservation_not_bound_to_prepared_request(
    mismatch: str,
) -> None:
    provider = successful_provider(ModelUsage(UsageState.KNOWN, 100, 50))
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )
    reservation = reservation_for(prepared)
    if mismatch == "digest":
        reservation = replace(reservation, prepared_request_digest="wrong")
    elif mismatch == "input_bound":
        reservation = replace(
            reservation,
            input_bound=reservation.input_bound + 1,
            amount=reservation.amount + 1,
        )
    elif mismatch == "output_max":
        reservation = replace(
            reservation,
            output_max=reservation.output_max + 1,
            amount=reservation.amount + 1,
        )
    elif mismatch == "state":
        reservation = replace(reservation, state=ReservationState.RELEASED)
    else:
        object.__setattr__(reservation, "amount", reservation.amount - 1)

    with pytest.raises(StableError) as caught:
        await gateway.send(provider, prepared, reservation=reservation)

    assert caught.value.code == "budget_reservation_rejected"
    assert provider.send_calls == 0
    assert not provider.owns_prepared_request(prepared)


def test_budget_rejection_can_discard_prepared_request_idempotently() -> None:
    provider = FakeModelProvider(capabilities())
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    gateway.discard_prepared(provider, prepared)
    gateway.discard_prepared(provider, prepared)

    assert not provider.owns_prepared_request(prepared)
    assert provider.active_prepare_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ("context_token_limit", "preflight_token_counting", "usage_mapping_trusted"),
)
async def test_gateway_rejects_capability_drift_after_prepare(field: str) -> None:
    provider = successful_provider(ModelUsage(UsageState.KNOWN, 100, 50))
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )
    if field == "context_token_limit":
        drifted = replace(provider.capabilities, context_token_limit=20_000)
    elif field == "preflight_token_counting":
        drifted = replace(provider.capabilities, preflight_token_counting=False)
    else:
        drifted = replace(provider.capabilities, usage_mapping_trusted=False)
    provider._capabilities = drifted

    with pytest.raises(StableError) as caught:
        await gateway.send(provider, prepared, reservation=reservation_for(prepared))

    assert caught.value.code == "model_capability_mismatch"
    assert provider.send_calls == 0


@pytest.mark.asyncio
async def test_usage_trust_is_bound_to_prepare_capability_during_send() -> None:
    class TrustSwitchingProvider(FakeModelProvider):
        async def send_prepared(
            self, request: PreparedModelRequest
        ) -> ProviderSendResult:
            self._capabilities = replace(self.capabilities, usage_mapping_trusted=True)
            return await super().send_prepared(request)

    provider = TrustSwitchingProvider(
        replace(capabilities(), usage_mapping_trusted=False),
        (
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={"findings": []},
                usage=ModelUsage(UsageState.KNOWN, 300, 250),
            ),
        ),
    )
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await gateway.send(
        provider, prepared, reservation=reservation_for(prepared)
    )

    assert outcome.reservation_action is ReservationAction.SETTLE_UNCERTAIN
    assert outcome.usage.state is UsageState.UNTRUSTED
    assert outcome.accounted_tokens == 550


@pytest.mark.asyncio
async def test_fake_provider_rejects_foreign_prepare_permit() -> None:
    first = FakeModelProvider(capabilities())
    second = FakeModelProvider(capabilities())
    prepared = first.prepare_request(
        envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    with pytest.raises(ValueError, match="prepare permit"):
        await second.send_prepared(prepared)

    assert second.send_calls == 0


@pytest.mark.asyncio
async def test_prepared_request_permit_cannot_be_replayed() -> None:
    provider = FakeModelProvider(
        capabilities(),
        (
            ProviderSendResult(
                provider_state=ProviderState.SUCCEEDED,
                request_sent=True,
                response_payload={"findings": []},
                usage=ModelUsage(UsageState.KNOWN, 100, 50),
            ),
        ),
    )
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )
    await gateway.send(provider, prepared, reservation=reservation_for(prepared))

    with pytest.raises(StableError, match="model_capability_mismatch"):
        await gateway.send(provider, prepared, reservation=reservation_for(prepared))

    assert provider.send_calls == 1


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
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await gateway.send(
        provider, prepared, reservation=reservation_for(prepared)
    )

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
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await gateway.send(
        provider, prepared, reservation=reservation_for(prepared)
    )

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
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    outcome = await gateway.send(
        provider, prepared, reservation=reservation_for(prepared)
    )

    assert outcome.state.provider_state is ProviderState.UNKNOWN
    assert outcome.error_code == "model_result_unknown"
    assert "secret" not in repr(outcome)


def test_capability_boundary_exception_is_mapped_without_secret_text() -> None:
    class BrokenCapabilitiesProvider:
        @property
        def capabilities(self) -> ModelCapabilities:
            raise RuntimeError("credential=top-secret")

        def prepare_request(
            self, envelope: PromptEnvelope, options: ModelRequestOptions
        ) -> PreparedModelRequest:
            raise AssertionError("must not prepare")

        def owns_prepared_request(self, request: PreparedModelRequest) -> bool:
            return False

        def discard_prepared(self, request: PreparedModelRequest) -> None:
            return None

        async def send_prepared(
            self, request: PreparedModelRequest
        ) -> ProviderSendResult:
            raise AssertionError("must not send")

    with pytest.raises(StableError) as caught:
        ModelGateway().prepare(
            BrokenCapabilitiesProvider(),
            envelope(),
            options(StructuredOutputStrategy.JSON_SCHEMA),
        )

    assert caught.value.code == "model_failed_known"
    assert "secret" not in repr(caught.value)


def test_prepare_owns_failure_discards_provider_permit() -> None:
    class BrokenOwnsProvider(FakeModelProvider):
        def owns_prepared_request(self, request: PreparedModelRequest) -> bool:
            raise RuntimeError("credential=top-secret")

    provider = BrokenOwnsProvider(capabilities())

    with pytest.raises(StableError) as caught:
        ModelGateway().prepare(
            provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
        )

    assert caught.value.code == "model_capability_mismatch"
    assert provider.active_prepare_count == 0
    assert "secret" not in repr(caught.value)


@pytest.mark.asyncio
async def test_send_capability_read_failure_is_stable_and_discards_permits() -> None:
    class BreakingCapabilitiesProvider(FakeModelProvider):
        break_capabilities = False

        @property
        def capabilities(self) -> ModelCapabilities:
            if self.break_capabilities:
                raise RuntimeError("credential=top-secret")
            return super().capabilities

    provider = BreakingCapabilitiesProvider(capabilities())
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )
    provider.break_capabilities = True

    with pytest.raises(StableError) as caught:
        await gateway.send(provider, prepared, reservation=reservation_for(prepared))

    assert caught.value.code == "model_failed_known"
    assert provider.active_prepare_count == 0
    assert provider.send_calls == 0
    assert "secret" not in repr(caught.value)


@pytest.mark.asyncio
async def test_invalid_provider_return_becomes_unknown_and_settles_reservation() -> (
    None
):
    class InvalidReturnProvider(FakeModelProvider):
        async def send_prepared(self, request: PreparedModelRequest) -> Any:
            self.discard_prepared(request)
            self.send_calls += 1
            return cast(Any, {"secret": "credential=top-secret"})

    provider = InvalidReturnProvider(capabilities())
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )
    reservation = reservation_for(prepared)

    outcome = await gateway.send(provider, prepared, reservation=reservation)

    assert outcome.state.provider_state is ProviderState.UNKNOWN
    assert outcome.reservation_action is ReservationAction.SETTLE_UNCERTAIN
    assert outcome.accounted_tokens == reservation.amount
    assert outcome.error_code == "model_result_unknown"
    assert "secret" not in repr(outcome)
    assert provider.active_prepare_count == 0


@pytest.mark.asyncio
async def test_cancelled_send_propagates_and_discards_both_permits() -> None:
    class CancelledProvider(FakeModelProvider):
        async def send_prepared(
            self, request: PreparedModelRequest
        ) -> ProviderSendResult:
            self.send_calls += 1
            raise asyncio.CancelledError

    provider = CancelledProvider(capabilities())
    gateway = ModelGateway()
    prepared = gateway.prepare(
        provider, envelope(), options(StructuredOutputStrategy.JSON_SCHEMA)
    )

    with pytest.raises(asyncio.CancelledError):
        await gateway.send(provider, prepared, reservation=reservation_for(prepared))

    assert provider.send_calls == 1
    assert provider.active_prepare_count == 0
    assert not provider.owns_prepared_request(prepared)


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


def capability_with_invalid_flag(
    field: str, invalid_value: object
) -> ModelCapabilities:
    baseline = capabilities()
    value = cast(Any, invalid_value)
    if field == "preflight_token_counting":
        return replace(baseline, preflight_token_counting=value)
    if field == "usage_mapping_trusted":
        return replace(baseline, usage_mapping_trusted=value)
    if field == "streaming_disabled":
        return replace(baseline, streaming_disabled=value)
    if field == "retries_disabled":
        return replace(baseline, retries_disabled=value)
    return replace(baseline, dynamic_tools_disabled=value)


def fake_provider_for_scenario(scenario: ProviderScenario) -> ModelGatewayPort:
    if scenario is ProviderScenario.EXCEPTION:

        class ExplodingProvider(FakeModelProvider):
            async def send_prepared(
                self, request: PreparedModelRequest
            ) -> ProviderSendResult:
                self.send_calls += 1
                raise RuntimeError("credential=top-secret")

        return ExplodingProvider(capabilities())

    if scenario is ProviderScenario.SUCCEEDED_KNOWN:
        result = ProviderSendResult(
            provider_state=ProviderState.SUCCEEDED,
            request_sent=True,
            response_payload={"findings": []},
            usage=ModelUsage(UsageState.KNOWN, 5, 3),
        )
    elif scenario is ProviderScenario.SUCCEEDED_MISSING:
        result = ProviderSendResult(
            provider_state=ProviderState.SUCCEEDED,
            request_sent=True,
            response_payload={"findings": []},
        )
    elif scenario is ProviderScenario.FAILED_UNSENT:
        result = ProviderSendResult(
            provider_state=ProviderState.FAILED_KNOWN,
            request_sent=False,
        )
    else:
        result = ProviderSendResult(provider_state=ProviderState.UNKNOWN)
    return FakeModelProvider(capabilities(), (result,))
