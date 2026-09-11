"""Reusable assertions for fake and real model Provider adapters."""

from __future__ import annotations

from code_review_agent.domain.execution.models import (
    ModelRequestOptions,
    PreparedModelRequest,
    PromptEnvelope,
)
from code_review_agent.ports.model import ModelGatewayPort

_AUTH_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key"}
)


def assert_provider_preparation_contract(
    provider: ModelGatewayPort,
    envelope: PromptEnvelope,
    options: ModelRequestOptions,
) -> tuple[PreparedModelRequest, PreparedModelRequest]:
    """Assert deterministic wire semantics without assuming a vendor body shape."""

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
        assert not (_AUTH_HEADERS & prepared.headers.keys())
        assert prepared.strategy is options.strategy
        assert prepared.output_token_max == options.max_output_tokens
        assert provider.owns_prepared_request(prepared)

    assert first.body == second.body
    assert first.body_digest == second.body_digest
    assert first.preparation_id != second.preparation_id
    return first, second
