"""Reusable assertions for fake and real model Provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

import pytest

from code_review_agent.adapters.model.gateway import ModelGateway
from code_review_agent.domain.budget.models import BudgetReservation, ReservationState
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.execution.models import (
    ModelCallOutcome,
    ModelRequestOptions,
    PreparedModelRequest,
    PromptEnvelope,
    ProviderState,
    ReservationAction,
    UsageState,
)
from code_review_agent.ports.model import ModelGatewayPort

_FIXED_HEADER_ALLOWLIST = frozenset(
    {"accept", "content-type", "anthropic-version", "anthropic-beta"}
)


class ProviderScenario(StrEnum):
    SUCCEEDED_KNOWN = "succeeded_known"
    SUCCEEDED_MISSING = "succeeded_missing"
    SUCCEEDED_UNTRUSTED = "succeeded_untrusted"
    FAILED_UNSENT = "failed_unsent"
    UNKNOWN = "unknown"
    EXCEPTION = "exception"


type ProviderFactory = Callable[[ProviderScenario], ModelGatewayPort]


def assert_provider_preparation_contract(
    provider: ModelGatewayPort,
    envelope: PromptEnvelope,
    options: ModelRequestOptions,
) -> tuple[PreparedModelRequest, PreparedModelRequest]:
    """Assert deterministic wire semantics without assuming a vendor body shape."""

    assert isinstance(provider, ModelGatewayPort)
    first = provider.prepare_request(envelope, options)
    second = provider.prepare_request(envelope, options)
    capabilities = provider.capabilities

    for prepared in (first, second):
        assert prepared.provider_id == capabilities.provider_id
        assert prepared.provider_version == capabilities.provider_version
        assert prepared.model_id == capabilities.model_id
        assert prepared.origin == capabilities.origin
        assert prepared.method == capabilities.request_method
        assert prepared.path == capabilities.request_path
        assert dict(prepared.headers) == dict(capabilities.fixed_headers)
        assert prepared.headers.keys() <= _FIXED_HEADER_ALLOWLIST
        assert prepared.strategy is options.strategy
        assert prepared.output_token_max == options.max_output_tokens
        assert provider.owns_prepared_request(prepared)

    assert first.body == second.body
    assert first.body_digest == second.body_digest
    assert first.preparation_id != second.preparation_id
    return first, second


async def assert_provider_send_contract(
    provider_factory: ProviderFactory,
    envelope: PromptEnvelope,
    options: ModelRequestOptions,
) -> None:
    """Exercise terminal states and one-shot permits for any Provider adapter."""

    for scenario in ProviderScenario:
        provider = provider_factory(scenario)
        gateway = ModelGateway()
        prepared = gateway.prepare(provider, envelope, options)
        reservation = _reservation_for(prepared)

        outcome = await gateway.send(provider, prepared, reservation=reservation)

        _assert_scenario_outcome(scenario, outcome, reservation.amount)
        assert not provider.owns_prepared_request(prepared)
        with pytest.raises(StableError, match="model_capability_mismatch"):
            await gateway.send(provider, prepared, reservation=reservation)


def _reservation_for(prepared: PreparedModelRequest) -> BudgetReservation:
    return BudgetReservation(
        reservation_id=f"contract-{prepared.preparation_id}",
        account_id="contract-budget",
        task_id="contract-task",
        model_call_id=f"contract-call-{prepared.preparation_id}",
        work_unit_id="contract-unit",
        input_bound=prepared.input_token_bound,
        output_max=prepared.output_token_max,
        amount=prepared.input_token_bound + prepared.output_token_max,
        prepared_request_digest=prepared.body_digest,
        capability_ref="contract-capability",
        state=ReservationState.ACTIVE,
    )


def _assert_scenario_outcome(
    scenario: ProviderScenario,
    outcome: ModelCallOutcome,
    reserved: int,
) -> None:
    if scenario is ProviderScenario.SUCCEEDED_KNOWN:
        assert outcome.state.provider_state is ProviderState.SUCCEEDED
        assert outcome.reservation_action is ReservationAction.SETTLE_KNOWN
        assert outcome.accounted_tokens == 8
    elif scenario is ProviderScenario.SUCCEEDED_UNTRUSTED:
        assert outcome.state.provider_state is ProviderState.SUCCEEDED
        assert outcome.reservation_action is ReservationAction.SETTLE_UNCERTAIN
        assert outcome.accounted_tokens == reserved
        assert outcome.usage.state is UsageState.UNTRUSTED
    elif scenario is ProviderScenario.FAILED_UNSENT:
        assert outcome.state.provider_state is ProviderState.FAILED_KNOWN
        assert outcome.reservation_action is ReservationAction.RELEASE
        assert outcome.accounted_tokens == 0
    else:
        assert outcome.reservation_action is ReservationAction.SETTLE_UNCERTAIN
        assert outcome.accounted_tokens == reserved
        assert outcome.usage.state is UsageState.MISSING
        if scenario in {ProviderScenario.UNKNOWN, ProviderScenario.EXCEPTION}:
            assert outcome.state.provider_state is ProviderState.UNKNOWN
