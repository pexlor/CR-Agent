"""Invariant-checking gateway for model provider adapters."""

from __future__ import annotations

from dataclasses import dataclass, replace

from code_review_agent.domain.budget.models import UsageState
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
        capabilities = provider.capabilities
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

        if not self._prepared_request_matches(prepared, provider, options):
            raise self._capability_error("model_prepare")
        try:
            provider_owns_request = provider.owns_prepared_request(prepared)
        except Exception:
            provider_owns_request = False
        if not provider_owns_request:
            raise self._capability_error("model_prepare")
        capability_snapshot = replace(capabilities)
        self._prepared[id(prepared)] = _PreparedPermit(
            provider=provider,
            request=prepared,
            fingerprint=self._request_fingerprint(prepared),
            capabilities=capability_snapshot,
            capability_digest=self._capability_fingerprint(capability_snapshot),
        )
        return prepared

    async def send(
        self,
        provider: ModelGatewayPort,
        request: PreparedModelRequest,
        *,
        reservation_tokens: int,
    ) -> ModelCallOutcome:
        if type(reservation_tokens) is not int or reservation_tokens <= 0:
            raise ValueError("reservation tokens must be a positive integer")
        permit = self._prepared.get(id(request))
        try:
            provider_owns_request = provider.owns_prepared_request(request)
        except Exception:
            provider_owns_request = False
        current_capabilities = provider.capabilities
        if (
            permit is None
            or permit.provider is not provider
            or permit.request is not request
            or permit.fingerprint != self._request_fingerprint(request)
            or not self._body_digest_matches(request)
            or current_capabilities != permit.capabilities
            or self._capability_fingerprint(current_capabilities)
            != permit.capability_digest
            or not provider_owns_request
            or not self._prepared_request_matches(
                request,
                provider,
                ModelRequestOptions(
                    strategy=request.strategy,
                    max_output_tokens=request.output_token_max,
                ),
            )
        ):
            raise self._capability_error("model_send")

        del self._prepared[id(request)]
        usage_mapping_trusted = permit.capabilities.usage_mapping_trusted

        try:
            result = await provider.send_prepared(request)
        except Exception:
            result = ProviderSendResult(
                provider_state=ProviderState.UNKNOWN,
                error_code="model_result_unknown",
            )
        return self._outcome(
            result,
            reservation_tokens,
            usage_mapping_trusted=usage_mapping_trusted,
        )

    @staticmethod
    def _prepared_request_matches(
        request: PreparedModelRequest,
        provider: ModelGatewayPort,
        options: ModelRequestOptions,
    ) -> bool:
        capabilities = provider.capabilities
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
    def _outcome(
        result: ProviderSendResult,
        reservation_tokens: int,
        *,
        usage_mapping_trusted: bool,
    ) -> ModelCallOutcome:
        if (
            result.provider_state is ProviderState.FAILED_KNOWN
            and result.request_sent is False
        ):
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
