"""Single-shot OpenAI-compatible HTTP model provider."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

import httpx

from code_review_agent.config import CliConfig
from code_review_agent.domain.common.digests import canonical_json, sha256_bytes
from code_review_agent.domain.execution.models import (
    ModelCapabilities,
    ModelRequestOptions,
    ModelUsage,
    PreparedModelRequest,
    PromptEnvelope,
    ProviderSendResult,
    ProviderState,
    StructuredOutputStrategy,
    UsageState,
)


@dataclass(frozen=True, slots=True)
class _Config:
    origin: str
    path: str
    api_key: str
    timeout_seconds: int
    max_response_bytes: int


class OpenAICompatibleProvider:
    """Adapt a strict OpenAI-compatible JSON endpoint to the model port."""

    def __init__(self, config: CliConfig) -> None:
        if config.provider_id != "openai-compatible" or not config.api_key:
            raise ValueError("provider_config_invalid")
        self._config = _Config(
            origin=config.provider_origin,
            path=config.provider_path,
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
            max_response_bytes=config.max_response_bytes,
        )
        self._capabilities = ModelCapabilities(
            provider_id=config.provider_id,
            provider_version=config.provider_version,
            model_id=config.model_id,
            origin=config.provider_origin,
            context_token_limit=10_000,
            max_output_tokens=config.max_output_tokens,
            structured_output_strategies=(StructuredOutputStrategy.JSON_SCHEMA,),
            preflight_token_counting=True,
            usage_mapping_trusted=True,
            streaming_disabled=True,
            retries_disabled=True,
            dynamic_tools_disabled=True,
            request_method="POST",
            request_path=config.provider_path,
            fixed_headers={
                "accept": "application/json",
                "content-type": "application/json",
            },
        )
        self._prepared: dict[str, PreparedModelRequest] = {}

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def prepare_request(
        self,
        envelope: PromptEnvelope,
        options: ModelRequestOptions,
    ) -> PreparedModelRequest:
        output_schema = canonical_json(dict(envelope.output_schema))
        system_rules = (
            f"{envelope.system_rules}\n"
            "Output JSON Schema (follow exactly):\n"
            f"{output_schema}"
        )
        body = canonical_json(
            {
                "model": self._capabilities.model_id,
                "messages": [
                    {"role": "system", "content": system_rules},
                    {
                        "role": "user",
                        "content": (
                            f"Review unit: {envelope.work_unit}\n"
                            f"Diff:\n{envelope.diff}\n"
                            f"Tool facts: {', '.join(envelope.tool_facts)}"
                        ),
                    },
                ],
                "temperature": 0,
                "stream": False,
                "response_format": {"type": "json_object"},
                "max_tokens": options.max_output_tokens,
            }
        ).encode("utf-8")
        request = PreparedModelRequest(
            provider_id=self._capabilities.provider_id,
            provider_version=self._capabilities.provider_version,
            model_id=self._capabilities.model_id,
            origin=self._capabilities.origin,
            method="POST",
            path=self._capabilities.request_path,
            headers=self._capabilities.fixed_headers,
            body=body,
            body_digest=sha256_bytes(body),
            preparation_id=str(uuid4()),
            strategy=options.strategy,
            input_token_bound=max(1, (len(body) + 3) // 4),
            output_token_max=options.max_output_tokens,
            streaming=False,
            automatic_retries=0,
            fallback_model_id=None,
            dynamic_tools=(),
        )
        self._prepared[request.preparation_id] = request
        return request

    def owns_prepared_request(self, request: PreparedModelRequest) -> bool:
        return self._prepared.get(request.preparation_id) is request

    def discard_prepared(self, request: PreparedModelRequest) -> None:
        self._prepared.pop(request.preparation_id, None)

    async def send_prepared(self, request: PreparedModelRequest) -> ProviderSendResult:
        if not self.owns_prepared_request(request):
            raise ValueError("request has no valid prepare permit")
        self.discard_prepared(request)
        headers = {
            **dict(request.headers),
            "authorization": f"Bearer {self._config.api_key}",
        }
        timeout = httpx.Timeout(self._config.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self._config.origin,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._config.path,
                    content=request.body,
                    headers=headers,
                )
                content = await response.aread()
        except (httpx.HTTPError, OSError):
            return _unknown("provider_transport_unknown")

        if len(content) > self._config.max_response_bytes:
            return _unknown("provider_response_too_large")
        if response.status_code in {408, 429} or response.status_code >= 500:
            error_code = (
                "provider_http_5xx"
                if response.status_code >= 500
                else f"provider_http_{response.status_code}"
            )
            return _unknown(error_code)
        if response.status_code < 200 or response.status_code >= 300:
            return _failed_known(f"provider_http_{response.status_code}")
        try:
            payload = json.loads(content.decode("utf-8"))
            content_text = payload["choices"][0]["message"]["content"]
            if not isinstance(content_text, str):
                raise ValueError
            model_payload = json.loads(content_text)
            if not isinstance(model_payload, dict):
                raise ValueError
        except (UnicodeDecodeError, KeyError, IndexError, TypeError, ValueError):
            return _unknown("provider_response_invalid")

        usage = _usage(payload.get("usage"))
        return ProviderSendResult(
            provider_state=ProviderState.SUCCEEDED,
            request_sent=True,
            response_payload=model_payload,
            usage=usage,
        )


def _usage(value: object) -> ModelUsage:
    if not isinstance(value, dict):
        return ModelUsage(UsageState.MISSING)
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    if (
        type(prompt) is int
        and prompt >= 0
        and type(completion) is int
        and completion >= 0
    ):
        return ModelUsage(UsageState.KNOWN, prompt, completion)
    return ModelUsage(UsageState.UNTRUSTED)


def _unknown(code: str) -> ProviderSendResult:
    return ProviderSendResult(
        provider_state=ProviderState.UNKNOWN,
        request_sent=True,
        usage=ModelUsage(UsageState.MISSING),
        error_code=code,
    )


def _failed_known(code: str) -> ProviderSendResult:
    return ProviderSendResult(
        provider_state=ProviderState.FAILED_KNOWN,
        request_sent=True,
        usage=ModelUsage(UsageState.MISSING),
        error_code=code,
    )
