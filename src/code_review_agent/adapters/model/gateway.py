"""Invariant-checking gateway for model provider adapters."""

from __future__ import annotations

from code_review_agent.domain.budget.models import UsageState
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.execution.models import (
    ModelCallOutcome,
    ModelCallState,
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


class ModelGateway:
    """Enforce fixed call behavior around a single Provider adapter invocation."""

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
            or options.strategy not in capabilities.structured_output_strategies
            or options.max_output_tokens > capabilities.max_output_tokens
        ):
            raise self._capability_error()

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
            raise self._capability_error()
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
        if not self._prepared_request_matches(
            request,
            provider,
            ModelRequestOptions(
                strategy=request.strategy,
                max_output_tokens=request.output_token_max,
            ),
        ):
            raise self._capability_error()

        try:
            result = await provider.send_prepared(request)
        except Exception:
            result = ProviderSendResult(
                provider_state=ProviderState.UNKNOWN,
                error_code="model_result_unknown",
            )
        return self._outcome(result, reservation_tokens)

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
            and request.strategy is options.strategy
            and request.output_token_max == options.max_output_tokens
            and request.input_token_bound + request.output_token_max
            <= capabilities.context_token_limit
            and not request.streaming
            and request.automatic_retries == 0
            and request.fallback_model_id is None
            and not request.dynamic_tools
        )

    @staticmethod
    def _outcome(
        result: ProviderSendResult, reservation_tokens: int
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
            result.provider_state is ProviderState.UNKNOWN
            and usage.state is UsageState.KNOWN
        ):
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
    def _capability_error() -> StableError:
        return StableError(
            code="model_capability_mismatch",
            category="provider",
            stage="model_prepare",
            recoverable=False,
            next_actions=("change_task_configuration",),
        )
