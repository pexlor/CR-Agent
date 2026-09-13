from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
import respx

from code_review_agent.adapters.model.openai_compatible import (
    OpenAICompatibleProvider,
)
from code_review_agent.config import CliConfig
from code_review_agent.domain.common.digests import canonical_json
from code_review_agent.domain.execution.models import (
    ModelRequestOptions,
    PromptEnvelope,
    ProviderState,
    StructuredOutputStrategy,
    UsageState,
)


def _config() -> CliConfig:
    return CliConfig(
        provider_id="openai-compatible",
        provider_version="1",
        model_id="review-model",
        provider_origin="https://api.example.com",
        provider_path="/v1/chat/completions",
        api_key="secret-token",
        timeout_seconds=30,
        max_response_bytes=1024,
        max_output_tokens=512,
    )


def _envelope() -> PromptEnvelope:
    return PromptEnvelope(
        system_rules="Review only this change.",
        work_unit="unit-1",
        diff="@@ -1 +1 @@\n-old\n+new",
        controlled_context=(),
        tool_facts=("no_tool_evidence",),
        prohibited_capabilities=("streaming", "retries"),
        version_digest="a" * 64,
        output_schema={"type": "object", "properties": {"findings": {"type": "array"}}},
    )


def _prepare(provider: OpenAICompatibleProvider):
    return provider.prepare_request(
        _envelope(),
        ModelRequestOptions(
            strategy=StructuredOutputStrategy.JSON_SCHEMA,
            max_output_tokens=256,
        ),
    )


def test_prepare_builds_fixed_request_without_exposing_token() -> None:
    provider = OpenAICompatibleProvider(_config())

    request = _prepare(provider)
    body = json.loads(request.body)

    assert request.path == "/v1/chat/completions"
    assert body["model"] == "review-model"
    assert body["stream"] is False
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert (
        canonical_json(dict(_envelope().output_schema))
        in body["messages"][0]["content"]
    )
    assert "secret-token" not in repr(request)
    assert "secret-token" not in request.body.decode()
    assert provider.owns_prepared_request(request)


@respx.mock
def test_send_maps_successful_response_and_usage() -> None:
    provider = OpenAICompatibleProvider(_config())
    request = _prepare(provider)
    route = respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"findings": []}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )

    result = asyncio.run(provider.send_prepared(request))

    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer secret-token"
    assert result.provider_state is ProviderState.SUCCEEDED
    assert dict(result.response_payload or {}) == {"findings": ()}
    assert result.usage.state is UsageState.KNOWN
    assert result.usage.reported_total == 15
    assert not provider.owns_prepared_request(request)


def test_send_disables_environment_proxy_inheritance() -> None:
    provider = OpenAICompatibleProvider(_config())
    request = _prepare(provider)

    with patch("httpx.AsyncClient", wraps=httpx.AsyncClient) as client, respx.mock:
        respx.post("https://api.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"findings": []}'}}],
                },
            )
        )
        asyncio.run(provider.send_prepared(request))

    assert client.call_args.kwargs["trust_env"] is False


@pytest.mark.parametrize(
    ("status", "state", "code"),
    [
        (401, ProviderState.FAILED_KNOWN, "provider_http_401"),
        (408, ProviderState.UNKNOWN, "provider_http_408"),
        (429, ProviderState.UNKNOWN, "provider_http_429"),
        (500, ProviderState.UNKNOWN, "provider_http_5xx"),
    ],
)
@respx.mock
def test_send_normalizes_http_errors(
    status: int,
    state: ProviderState,
    code: str,
) -> None:
    provider = OpenAICompatibleProvider(_config())
    request = _prepare(provider)
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(status, text="secret-token must not leak")
    )

    result = asyncio.run(provider.send_prepared(request))

    assert result.provider_state is state
    assert result.error_code == code
    assert "secret-token" not in repr(result)


@respx.mock
def test_send_returns_unknown_for_timeout_and_invalid_json() -> None:
    timeout_provider = OpenAICompatibleProvider(_config())
    timeout_request = _prepare(timeout_provider)
    respx.post("https://api.example.com/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    timeout = asyncio.run(timeout_provider.send_prepared(timeout_request))

    assert timeout.provider_state is ProviderState.UNKNOWN
    assert timeout.error_code == "provider_transport_unknown"

    invalid_provider = OpenAICompatibleProvider(_config())
    invalid_request = _prepare(invalid_provider)
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, text="not-json")
    )

    invalid = asyncio.run(invalid_provider.send_prepared(invalid_request))

    assert invalid.provider_state is ProviderState.UNKNOWN
    assert invalid.error_code == "provider_response_invalid"


@respx.mock
def test_prepared_request_can_only_be_sent_once() -> None:
    provider = OpenAICompatibleProvider(_config())
    request = _prepare(provider)
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"findings": []}'}}],
            },
        )
    )

    asyncio.run(provider.send_prepared(request))

    with pytest.raises(ValueError, match="prepare permit"):
        asyncio.run(provider.send_prepared(request))
