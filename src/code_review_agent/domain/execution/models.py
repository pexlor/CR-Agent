"""Provider-independent model call contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from code_review_agent.domain.budget.models import UsageState as UsageState
from code_review_agent.domain.common.digests import sha256_bytes

_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


class ProviderState(StrEnum):
    PENDING = "pending"
    RESERVED = "reserved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_KNOWN = "failed_known"
    UNKNOWN = "unknown"


class ResponseState(StrEnum):
    NOT_AVAILABLE = "not_available"
    PENDING = "pending"
    ACCEPTED = "accepted"
    INVALID_OUTPUT = "invalid_output"
    SECURITY_REJECTED = "security_rejected"


class StructuredOutputStrategy(StrEnum):
    JSON_SCHEMA = "json_schema"
    TOOL_CALLING = "tool_calling"
    JSON_TEXT = "json_text"


class ReservationAction(StrEnum):
    RELEASE = "release"
    SETTLE_KNOWN = "settle_known"
    SETTLE_UNCERTAIN = "settle_uncertain"


@dataclass(frozen=True, slots=True)
class ModelCallState:
    provider_state: ProviderState
    response_state: ResponseState

    def __post_init__(self) -> None:
        if self.provider_state is ProviderState.SUCCEEDED:
            if self.response_state is ResponseState.NOT_AVAILABLE:
                raise ValueError("provider success requires a response state")
        elif self.response_state is not ResponseState.NOT_AVAILABLE:
            raise ValueError("response state requires provider success")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    state: UsageState
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens)
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in values
        ):
            raise ValueError("usage counts must be non-negative integers")
        if self.state is UsageState.KNOWN and any(value is None for value in values):
            raise ValueError("known usage requires both token counts")
        if self.state is UsageState.MISSING and any(
            value is not None for value in values
        ):
            raise ValueError("missing usage cannot contain token counts")
        if self.state is UsageState.UNTRUSTED and (
            (self.input_tokens is None) is not (self.output_tokens is None)
        ):
            raise ValueError("untrusted usage requires both or neither token count")

    @property
    def reported_total(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    provider_id: str
    provider_version: str
    model_id: str
    origin: str
    context_token_limit: int
    max_output_tokens: int
    structured_output_strategies: tuple[StructuredOutputStrategy, ...]
    preflight_token_counting: bool
    usage_mapping_trusted: bool
    streaming_disabled: bool
    retries_disabled: bool
    dynamic_tools_disabled: bool

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.provider_id,
                self.provider_version,
                self.model_id,
                self.origin,
            )
        ):
            raise ValueError("model capability identity is required")
        if not self.origin.startswith("https://"):
            raise ValueError("model provider origin must use HTTPS")
        if (
            type(self.context_token_limit) is not int
            or type(self.max_output_tokens) is not int
            or self.context_token_limit <= 0
            or self.max_output_tokens <= 0
        ):
            raise ValueError("model token limits must be positive integers")
        strategies = tuple(self.structured_output_strategies)
        if not strategies or len(set(strategies)) != len(strategies):
            raise ValueError(
                "structured output strategies must be unique and non-empty"
            )
        object.__setattr__(self, "structured_output_strategies", strategies)


@dataclass(frozen=True, slots=True)
class PromptEnvelope:
    system_rules: str
    work_unit: str
    diff: str
    tool_facts: tuple[str, ...]
    output_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.system_rules or not self.work_unit:
            raise ValueError("prompt rules and work unit are required")
        if not isinstance(self.diff, str):
            raise TypeError("prompt diff must be text")
        facts = tuple(self.tool_facts)
        if any(not isinstance(fact, str) or not fact for fact in facts):
            raise ValueError("tool facts must be non-empty strings")
        schema = dict(self.output_schema)
        if schema.get("type") != "object":
            raise ValueError("output schema must describe an object")
        object.__setattr__(self, "tool_facts", facts)
        object.__setattr__(self, "output_schema", MappingProxyType(schema))


@dataclass(frozen=True, slots=True)
class ModelRequestOptions:
    strategy: StructuredOutputStrategy
    max_output_tokens: int
    streaming: bool = False
    automatic_retries: int = 0
    fallback_model_id: str | None = None
    dynamic_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("max output tokens must be a positive integer")
        if type(self.streaming) is not bool:
            raise TypeError("streaming must be a bool")
        if type(self.automatic_retries) is not int or self.automatic_retries < 0:
            raise ValueError("automatic retries must be a non-negative integer")
        if self.fallback_model_id is not None and not self.fallback_model_id:
            raise ValueError("fallback model ID cannot be empty")
        tools = tuple(self.dynamic_tools)
        if any(not isinstance(tool, str) or not tool for tool in tools):
            raise ValueError("dynamic tools must be non-empty strings")
        object.__setattr__(self, "dynamic_tools", tools)


@dataclass(frozen=True, slots=True)
class PreparedModelRequest:
    provider_id: str
    provider_version: str
    model_id: str
    origin: str
    method: str
    path: str
    header_names: tuple[str, ...]
    body: bytes
    body_digest: str
    strategy: StructuredOutputStrategy
    input_token_bound: int
    output_token_max: int
    streaming: bool
    automatic_retries: int
    fallback_model_id: str | None
    dynamic_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.provider_id,
                self.provider_version,
                self.model_id,
                self.origin,
                self.method,
                self.path,
                self.body_digest,
            )
        ):
            raise ValueError("prepared request identity is required")
        if not isinstance(self.body, bytes) or not self.body:
            raise ValueError("prepared request body must be non-empty bytes")
        if sha256_bytes(self.body) != self.body_digest:
            raise ValueError("prepared request body digest mismatch")
        if (
            type(self.input_token_bound) is not int
            or type(self.output_token_max) is not int
            or self.input_token_bound < 0
            or self.output_token_max <= 0
        ):
            raise ValueError("prepared request token bounds are invalid")
        object.__setattr__(self, "header_names", tuple(self.header_names))
        object.__setattr__(self, "dynamic_tools", tuple(self.dynamic_tools))


@dataclass(frozen=True, slots=True)
class ProviderSendResult:
    provider_state: ProviderState
    request_sent: bool | None = None
    response_payload: Mapping[str, object] | None = None
    usage: ModelUsage = field(default_factory=lambda: ModelUsage(UsageState.MISSING))
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.provider_state not in {
            ProviderState.SUCCEEDED,
            ProviderState.FAILED_KNOWN,
            ProviderState.UNKNOWN,
        }:
            raise ValueError("provider result must be terminal")
        if self.provider_state is ProviderState.SUCCEEDED:
            if self.request_sent is not True or self.response_payload is None:
                raise ValueError(
                    "provider success requires a sent request and response"
                )
        elif self.response_payload is not None:
            raise ValueError("provider response is only valid after success")
        if self.provider_state is ProviderState.UNKNOWN and self.request_sent is False:
            raise ValueError("an unsent request cannot have unknown provider state")
        if self.error_code is not None and not _STABLE_CODE.fullmatch(self.error_code):
            raise ValueError("provider error code must be a stable name")
        if self.response_payload is not None:
            object.__setattr__(
                self, "response_payload", MappingProxyType(dict(self.response_payload))
            )


@dataclass(frozen=True, slots=True)
class ModelCallOutcome:
    state: ModelCallState
    reservation_action: ReservationAction
    usage: ModelUsage
    accounted_tokens: int
    overage_tokens: int
    response_payload: Mapping[str, object] | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.accounted_tokens < 0 or self.overage_tokens < 0:
            raise ValueError("accounted usage and overage must be non-negative")
        if self.response_payload is not None:
            object.__setattr__(
                self, "response_payload", MappingProxyType(dict(self.response_payload))
            )
