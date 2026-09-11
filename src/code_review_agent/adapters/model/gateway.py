"""Invariant-checking gateway for model provider adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from code_review_agent.domain.budget.models import (
    BudgetReservation,
    ReservationState,
    UsageState,
)
from code_review_agent.domain.common.digests import sha256_bytes, sha256_digest
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.execution.models import (
    ModelCallOutcome,
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
)
from code_review_agent.ports.model import ModelGatewayPort


@dataclass(frozen=True, slots=True)
class _PreparedPermit:
    provider: ModelGatewayPort
    request: PreparedModelRequest
    fingerprint: str
    capabilities: ModelCapabilities
    capability_digest: str


class ModelGateway:
    """Enforce fixed call behavior around a single Provider adapter invocation."""

    def __init__(self) -> None:
        self._prepared: dict[int, _PreparedPermit] = {}

    def prepare(
        self,
        provider: ModelGatewayPort,
        envelope: PromptEnvelope,
        options: ModelRequestOptions,
    ) -> PreparedModelRequest:
        try:
            capabilities = provider.capabilities
        except Exception:
            raise self._provider_error("model_prepare") from None
        if not isinstance(capabilities, ModelCapabilities):
            raise self._provider_error("model_prepare")
        if (
            options.streaming
            or options.automatic_retries != 0
            or options.fallback_model_id is not None
            or options.dynamic_tools
            or not capabilities.streaming_disabled
            or not capabilities.retries_disabled
            or not capabilities.dynamic_tools_disabled
            or not capabilities.preflight_token_counting
            or options.strategy not in capabilities.structured_output_strategies
            or options.max_output_tokens > capabilities.max_output_tokens
        ):
            raise self._capability_error("model_prepare")

        try:
            prepared = provider.prepare_request(envelope, options)
        except StableError:
            raise
        except Exception:
            raise StableError(
                code="model_failed_known",
                category="provider",
                stage="model_prepare",
                recoverable=True,
                next_actions=("retry_explicitly",),
            ) from None

        try:
            request_matches = self._prepared_request_matches(
                prepared, capabilities, options
            )
            provider_owns_request = provider.owns_prepared_request(prepared)
        except Exception:
            self._discard_provider(provider, prepared)
            raise self._capability_error("model_prepare") from None
        if not request_matches or provider_owns_request is not True:
            self._discard_provider(provider, prepared)
            raise self._capability_error("model_prepare")
        try:
            capability_snapshot = replace(capabilities)
            capability_digest = self._capability_fingerprint(capability_snapshot)
            request_fingerprint = self._request_fingerprint(prepared)
        except Exception:
            self._discard_provider(provider, prepared)
            raise self._capability_error("model_prepare") from None
        self._prepared[id(prepared)] = _PreparedPermit(
            provider=provider,
            request=prepared,
            fingerprint=request_fingerprint,
            capabilities=capability_snapshot,
            capability_digest=capability_digest,
        )
        return prepared

    async def send(
        self,
        provider: ModelGatewayPort,
        request: PreparedModelRequest,
        *,
        reservation: BudgetReservation,
    ) -> ModelCallOutcome:
        permit = self._prepared.get(id(request))
        try:
            provider_owns_request = provider.owns_prepared_request(request)
        except Exception:
            self.discard_prepared(provider, request)
            raise self._provider_error("model_send") from None
        try:
            current_capabilities = provider.capabilities
        except Exception:
            self.discard_prepared(provider, request)
            raise self._provider_error("model_send") from None
        try:
            request_is_valid = (
                permit is not None
                and permit.provider is provider
                and permit.request is request
                and permit.fingerprint == self._request_fingerprint(request)
                and self._body_digest_matches(request)
                and isinstance(current_capabilities, ModelCapabilities)
                and current_capabilities == permit.capabilities
                and self._capability_fingerprint(current_capabilities)
                == permit.capability_digest
                and provider_owns_request is True
                and self._prepared_request_matches(
                    request,
                    permit.capabilities,
                    ModelRequestOptions(
                        strategy=request.strategy,
                        max_output_tokens=request.output_token_max,
                    ),
                )
            )
        except Exception:
            request_is_valid = False
        if not request_is_valid or permit is None:
            self.discard_prepared(provider, request)
            raise self._capability_error("model_send")
        if not self._reservation_matches(reservation, request):
            self.discard_prepared(provider, request)
            raise self._reservation_error()

        del self._prepared[id(request)]
        usage_mapping_trusted = permit.capabilities.usage_mapping_trusted

        try:
            result = await provider.send_prepared(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._unknown_outcome(reservation.amount)
        finally:
            self._discard_provider(provider, request)
        if type(result) is not ProviderSendResult:
            return self._unknown_outcome(
                reservation.amount,
                reported_usage=self._reported_usage_as_untrusted(result),
            )
        try:
            return self._outcome(
                result,
                reservation.amount,
                usage_mapping_trusted=usage_mapping_trusted,
            )
        except Exception:
            return self._unknown_outcome(
                reservation.amount,
                reported_usage=self._reported_usage_as_untrusted(result),
            )

    def discard_prepared(
        self, provider: ModelGatewayPort, request: PreparedModelRequest
    ) -> None:
        """Idempotently release Gateway and Provider preparation state."""

        permit = self._prepared.pop(id(request), None)
        discarded = self._discard_provider(provider, request)
        if permit is not None and permit.provider is not provider:
            discarded = self._discard_provider(permit.provider, request) and discarded
        if not discarded:
            raise self._provider_error("model_discard")

    @staticmethod
    def _prepared_request_matches(
        request: PreparedModelRequest,
        capabilities: ModelCapabilities,
        options: ModelRequestOptions,
    ) -> bool:
        return (
            request.provider_id == capabilities.provider_id
            and request.provider_version == capabilities.provider_version
            and request.model_id == capabilities.model_id
            and request.origin == capabilities.origin
            and request.method == capabilities.request_method
            and request.path == capabilities.request_path
            and dict(request.headers) == dict(capabilities.fixed_headers)
            and request.strategy is options.strategy
            and request.strategy in capabilities.structured_output_strategies
            and request.output_token_max == options.max_output_tokens
            and request.output_token_max <= capabilities.max_output_tokens
            and request.input_token_bound + request.output_token_max
            <= capabilities.context_token_limit
            and not request.streaming
            and request.automatic_retries == 0
            and request.fallback_model_id is None
            and not request.dynamic_tools
            and capabilities.preflight_token_counting
            and capabilities.streaming_disabled
            and capabilities.retries_disabled
            and capabilities.dynamic_tools_disabled
        )

    @staticmethod
    def _body_digest_matches(request: PreparedModelRequest) -> bool:
        return isinstance(request.body, bytes) and (
            sha256_bytes(request.body) == request.body_digest
        )

    @staticmethod
    def _capability_fingerprint(capabilities: ModelCapabilities) -> str:
        return sha256_digest(
            {
                "context_token_limit": capabilities.context_token_limit,
                "dynamic_tools_disabled": capabilities.dynamic_tools_disabled,
                "fixed_headers": dict(capabilities.fixed_headers),
                "max_output_tokens": capabilities.max_output_tokens,
                "model_id": capabilities.model_id,
                "origin": capabilities.origin,
                "preflight_token_counting": capabilities.preflight_token_counting,
                "provider_id": capabilities.provider_id,
                "provider_version": capabilities.provider_version,
                "request_method": capabilities.request_method,
                "request_path": capabilities.request_path,
                "retries_disabled": capabilities.retries_disabled,
                "streaming_disabled": capabilities.streaming_disabled,
                "structured_output_strategies": [
                    strategy.value
                    for strategy in capabilities.structured_output_strategies
                ],
                "usage_mapping_trusted": capabilities.usage_mapping_trusted,
            }
        )

    @staticmethod
    def _request_fingerprint(request: PreparedModelRequest) -> str:
        return sha256_digest(
            {
                "automatic_retries": request.automatic_retries,
                "body_digest": request.body_digest,
                "dynamic_tools": list(request.dynamic_tools),
                "fallback_model_id": request.fallback_model_id,
                "headers": dict(request.headers),
                "input_token_bound": request.input_token_bound,
                "method": request.method,
                "model_id": request.model_id,
                "origin": request.origin,
                "output_token_max": request.output_token_max,
                "path": request.path,
                "preparation_id": request.preparation_id,
                "provider_id": request.provider_id,
                "provider_version": request.provider_version,
                "strategy": request.strategy.value,
                "streaming": request.streaming,
            }
        )

    @staticmethod
    def _reservation_matches(
        reservation: BudgetReservation, request: PreparedModelRequest
    ) -> bool:
        if not isinstance(reservation, BudgetReservation):
            return False
        expected_amount = request.input_token_bound + request.output_token_max
        return (
            reservation.state is ReservationState.ACTIVE
            and reservation.prepared_request_digest == request.body_digest
            and reservation.input_bound == request.input_token_bound
            and reservation.output_max == request.output_token_max
            and reservation.amount == expected_amount
        )

    @staticmethod
    def _discard_provider(
        provider: ModelGatewayPort, request: PreparedModelRequest
    ) -> bool:
        try:
            provider.discard_prepared(request)
        except Exception:
            return False
        return True

    @staticmethod
    def _unknown_outcome(
        reservation_tokens: int,
        *,
        reported_usage: ModelUsage | None = None,
    ) -> ModelCallOutcome:
        usage = reported_usage or ModelUsage(UsageState.MISSING)
        accounted_tokens = max(
            reservation_tokens,
            usage.reported_total or 0,
        )
        return ModelCallOutcome(
            state=ModelCallState(ProviderState.UNKNOWN, ResponseState.NOT_AVAILABLE),
            reservation_action=ReservationAction.SETTLE_UNCERTAIN,
            usage=usage,
            accounted_tokens=accounted_tokens,
            overage_tokens=0,
            error_code="model_result_unknown",
        )

    @staticmethod
    def _reported_usage_as_untrusted(result: object) -> ModelUsage | None:
        try:
            usage = result.usage  # type: ignore[attr-defined]
        except Exception:
            return None
        if type(usage) is not ModelUsage:
            return None
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or input_tokens < 0
            or output_tokens < 0
        ):
            return None
        return ModelUsage(
            UsageState.UNTRUSTED,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    @staticmethod
    def _outcome(
        result: ProviderSendResult,
        reservation_tokens: int,
        *,
        usage_mapping_trusted: bool,
    ) -> ModelCallOutcome:
        ProviderSendResult.__post_init__(result)
        if (
            result.provider_state is ProviderState.FAILED_KNOWN
            and result.request_sent is False
        ):
            if result.usage.state is not UsageState.MISSING:
                raise ValueError("unsent failure usage is contradictory")
            return ModelCallOutcome(
                state=ModelCallState(
                    ProviderState.FAILED_KNOWN, ResponseState.NOT_AVAILABLE
                ),
                reservation_action=ReservationAction.RELEASE,
                usage=ModelUsage(UsageState.MISSING),
                accounted_tokens=0,
                overage_tokens=0,
                error_code=result.error_code or "model_failed_known",
            )

        response_state = (
            ResponseState.PENDING
            if result.provider_state is ProviderState.SUCCEEDED
            else ResponseState.NOT_AVAILABLE
        )
        usage = result.usage
        if (
            result.provider_state is ProviderState.UNKNOWN or not usage_mapping_trusted
        ) and usage.state is UsageState.KNOWN:
            usage = ModelUsage(
                UsageState.UNTRUSTED,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        reported_total = usage.reported_total
        if usage.state is UsageState.KNOWN:
            if reported_total is None:  # Protected by ModelUsage, kept exhaustive.
                raise AssertionError("known usage is incomplete")
            action = ReservationAction.SETTLE_KNOWN
            accounted = reported_total
            overage = max(0, reported_total - reservation_tokens)
        else:
            action = ReservationAction.SETTLE_UNCERTAIN
            accounted = max(reservation_tokens, reported_total or 0)
            overage = 0

        error_code = result.error_code
        if result.provider_state is ProviderState.UNKNOWN:
            error_code = "model_result_unknown"
        elif result.provider_state is ProviderState.FAILED_KNOWN:
            error_code = error_code or "model_failed_known"

        return ModelCallOutcome(
            state=ModelCallState(result.provider_state, response_state),
            reservation_action=action,
            usage=usage,
            accounted_tokens=accounted,
            overage_tokens=overage,
            response_payload=result.response_payload,
            error_code=error_code,
        )

    @staticmethod
    def _capability_error(stage: str) -> StableError:
        return StableError(
            code="model_capability_mismatch",
            category="provider",
            stage=stage,
            recoverable=False,
            next_actions=("change_task_configuration",),
        )

    @staticmethod
    def _provider_error(stage: str) -> StableError:
        return StableError(
            code="model_failed_known",
            category="provider",
            stage=stage,
            recoverable=True,
            next_actions=("retry_explicitly",),
        )

    @staticmethod
    def _reservation_error() -> StableError:
        return StableError(
            code="budget_reservation_rejected",
            category="budget",
            stage="model_send",
            recoverable=True,
            next_actions=("reserve_budget",),
        )
