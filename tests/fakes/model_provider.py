"""Scriptable model provider used by gateway contract tests."""

from __future__ import annotations

from collections import deque

from code_review_agent.domain.common.digests import canonical_json, sha256_bytes
from code_review_agent.domain.execution.models import (
    ModelCapabilities,
    ModelRequestOptions,
    PreparedModelRequest,
    PromptEnvelope,
    ProviderSendResult,
)


class FakeModelProvider:
    """A deterministic implementation of the production model gateway port."""

    def __init__(
        self,
        capabilities: ModelCapabilities,
        results: tuple[ProviderSendResult, ...] = (),
    ) -> None:
        self._capabilities = capabilities
        self._results = deque(results)
        self.prepare_calls = 0
        self.send_calls = 0
        self.sent_requests: list[PreparedModelRequest] = []

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def prepare_request(
        self, envelope: PromptEnvelope, options: ModelRequestOptions
    ) -> PreparedModelRequest:
        self.prepare_calls += 1
        body = canonical_json(
            {
                "diff": envelope.diff,
                "output_schema": dict(envelope.output_schema),
                "strategy": options.strategy.value,
                "system_rules": envelope.system_rules,
                "tool_facts": list(envelope.tool_facts),
                "work_unit": envelope.work_unit,
            }
        ).encode("utf-8")
        return PreparedModelRequest(
            provider_id=self.capabilities.provider_id,
            provider_version=self.capabilities.provider_version,
            model_id=self.capabilities.model_id,
            origin=self.capabilities.origin,
            method="POST",
            path="/model",
            header_names=("content-type",),
            body=body,
            body_digest=sha256_bytes(body),
            strategy=options.strategy,
            input_token_bound=len(body),
            output_token_max=options.max_output_tokens,
            streaming=False,
            automatic_retries=0,
            fallback_model_id=None,
            dynamic_tools=(),
        )

    async def send_prepared(self, request: PreparedModelRequest) -> ProviderSendResult:
        self.send_calls += 1
        self.sent_requests.append(request)
        if not self._results:
            raise AssertionError("fake provider has no scripted result")
        return self._results.popleft()
