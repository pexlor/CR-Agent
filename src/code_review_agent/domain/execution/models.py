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
_FIXED_HEADER_ALLOWLIST = frozenset(
    {"accept", "content-type", "anthropic-version", "anthropic-beta"}
)


def _deep_freeze_json(value: object) -> object:
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be strings")
            frozen[key] = _deep_freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze_json(item) for item in value)
    raise TypeError("value must be JSON-compatible")


def _freeze_headers(headers: Mapping[str, str]) -> Mapping[str, str]:
    normalized = dict(headers)
    if not normalized:
        raise ValueError("fixed non-authentication headers are required")
    for name, value in normalized.items():
        if not isinstance(name, str) or name not in _FIXED_HEADER_ALLOWLIST:
            raise ValueError("prepared header name is not in the non-auth allowlist")
        if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
            raise ValueError("prepared header values must be fixed safe strings")
    return MappingProxyType(normalized)


def _validate_wire_target(method: str, path: str) -> None:
    if method != "POST":
        raise ValueError("model request method must be fixed to POST")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "://" in path
        or "?" in path
        or "#" in path
    ):
        raise ValueError("model request path must be a fixed relative API path")


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
    request_method: str
    request_path: str
    fixed_headers: Mapping[str, str]

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
        boolean_flags = (
            self.preflight_token_counting,
            self.usage_mapping_trusted,
            self.streaming_disabled,
            self.retries_disabled,
            self.dynamic_tools_disabled,
        )
        if any(type(value) is not bool for value in boolean_flags):
            raise TypeError("model capability flags must be bool values")
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
        _validate_wire_target(self.request_method, self.request_path)
        object.__setattr__(self, "fixed_headers", _freeze_headers(self.fixed_headers))


@dataclass(frozen=True, slots=True)
class PromptEnvelope:
    system_rules: str
    work_unit: str
    diff: str
    controlled_context: tuple[str, ...]
    tool_facts: tuple[str, ...]
    prohibited_capabilities: tuple[str, ...]
    version_digest: str
    output_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.system_rules or not self.work_unit or not self.version_digest:
            raise ValueError("prompt rules, work unit, and version digest are required")
        if not isinstance(self.diff, str):
            raise TypeError("prompt diff must be text")
        context = tuple(self.controlled_context)
        facts = tuple(self.tool_facts)
        prohibited = tuple(self.prohibited_capabilities)
        if any(
            not isinstance(item, str) or not item
            for item in (*context, *facts, *prohibited)
        ):
            raise ValueError("prompt list fields must contain non-empty strings")
        if not prohibited:
            raise ValueError("prompt prohibited capabilities are required")
        schema = _deep_freeze_json(self.output_schema)
        if not isinstance(schema, Mapping):
            raise TypeError("output schema must be an object")
        if schema.get("type") != "object":
            raise ValueError("output schema must describe an object")
        object.__setattr__(self, "controlled_context", context)
        object.__setattr__(self, "tool_facts", facts)
        object.__setattr__(self, "prohibited_capabilities", prohibited)
        object.__setattr__(self, "output_schema", schema)


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
    headers: Mapping[str, str]
    body: bytes
    body_digest: str
    preparation_id: str
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
                self.preparation_id,
            )
        ):
            raise ValueError("prepared request identity is required")
        if not isinstance(self.body, bytes) or not self.body:
            raise ValueError("prepared request body must be non-empty bytes")
        if sha256_bytes(self.body) != self.body_digest:
            raise ValueError("prepared request body digest mismatch")
        _validate_wire_target(self.method, self.path)
        if (
            type(self.input_token_bound) is not int
            or type(self.output_token_max) is not int
            or self.input_token_bound < 0
            or self.output_token_max <= 0
        ):
            raise ValueError("prepared request token bounds are invalid")
        object.__setattr__(self, "headers", _freeze_headers(self.headers))
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
