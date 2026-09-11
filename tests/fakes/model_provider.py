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
        self._prepared_requests: dict[str, PreparedModelRequest] = {}

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def prepare_request(
        self, envelope: PromptEnvelope, options: ModelRequestOptions
    ) -> PreparedModelRequest:
        self.prepare_calls += 1
        preparation_id = f"fake-prepared-{self.prepare_calls}"
        schema = dict(envelope.output_schema)
        if options.strategy.value == "json_schema":
            structured_output: dict[str, object] = {
                "mode": "json_schema",
                "schema": schema,
            }
        elif options.strategy.value == "tool_calling":
            structured_output = {
                "mode": "tool_calling",
                "tool": {"name": "submit_review", "input_schema": schema},
            }
        else:
            structured_output = {
                "mode": "json_text",
                "instruction": "return_json_only",
                "schema": schema,
            }
        body = canonical_json(
            {
                "controlled_context": list(envelope.controlled_context),
                "diff": envelope.diff,
                "prohibited_capabilities": list(envelope.prohibited_capabilities),
                "strategy": options.strategy.value,
                "structured_output": structured_output,
                "system_rules": envelope.system_rules,
                "tool_facts": list(envelope.tool_facts),
                "version_digest": envelope.version_digest,
                "work_unit": envelope.work_unit,
            }
        ).encode("utf-8")
        prepared = PreparedModelRequest(
            provider_id=self.capabilities.provider_id,
            provider_version=self.capabilities.provider_version,
            model_id=self.capabilities.model_id,
            origin=self.capabilities.origin,
            method=self.capabilities.request_method,
            path=self.capabilities.request_path,
            headers=self.capabilities.fixed_headers,
            body=body,
            body_digest=sha256_bytes(body),
            preparation_id=preparation_id,
            strategy=options.strategy,
            input_token_bound=len(body),
            output_token_max=options.max_output_tokens,
            streaming=False,
            automatic_retries=0,
            fallback_model_id=None,
            dynamic_tools=(),
        )
        self._prepared_requests[preparation_id] = prepared
        return prepared

    def owns_prepared_request(self, request: PreparedModelRequest) -> bool:
        return self._prepared_requests.get(request.preparation_id) is request

    async def send_prepared(self, request: PreparedModelRequest) -> ProviderSendResult:
        if not self.owns_prepared_request(request):
            raise ValueError("request has no valid prepare permit")
        del self._prepared_requests[request.preparation_id]
        self.send_calls += 1
        self.sent_requests.append(request)
        if not self._results:
            raise AssertionError("fake provider has no scripted result")
        return self._results.popleft()
