"""Provider adapter contract for immutable model requests."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from code_review_agent.domain.execution.models import (
    ModelCapabilities,
    ModelRequestOptions,
    PreparedModelRequest,
    PromptEnvelope,
    ProviderSendResult,
)


@runtime_checkable
class ModelGatewayPort(Protocol):
    """Shared contract implemented by fake and real Provider adapters."""

    @property
    def capabilities(self) -> ModelCapabilities:
        """Return immutable capabilities for the configured provider and model."""

    def prepare_request(
        self, envelope: PromptEnvelope, options: ModelRequestOptions
    ) -> PreparedModelRequest:
        """Serialize the final request exactly once without external I/O."""

    async def send_prepared(self, request: PreparedModelRequest) -> ProviderSendResult:
        """Send the prepared bytes once and return normalized terminal facts."""
