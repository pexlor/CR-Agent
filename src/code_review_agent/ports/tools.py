"""Ports and immutable contracts for declarative review tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from code_review_agent.domain.common.digests import sha256_digest

type RuleParameter = str | int | tuple[str, ...]
SHA256_HEX_LENGTH = 64


@dataclass(frozen=True, slots=True)
class ToolToken:
    value: str
    kind: str
    line: int
    column: int


def deterministic_tool_tokens(text: str) -> tuple[ToolToken, ...]:
    """Lex text once for both resource limits and finite-language operations."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    tokens: list[ToolToken] = []
    offset = 0
    line = 1
    column = 1
    while offset < len(text):
        char = text[offset]
        if char == "\n":
            offset += 1
            line += 1
            column = 1
            continue
        if char.isspace():
            offset += 1
            column += 1
            continue
        start_line, start_column = line, column
        if char.isalpha() or char == "_":
            start = offset
            while offset < len(text) and (
                text[offset].isalnum() or text[offset] == "_"
            ):
                offset += 1
                column += 1
            tokens.append(
                ToolToken(text[start:offset], "identifier", start_line, start_column)
            )
            continue
        if char in ("'", '"'):
            delimiter = char * 3 if text.startswith(char * 3, offset) else char
            offset += len(delimiter)
            column += len(delimiter)
            literal: list[str] = []
            while offset < len(text) and not text.startswith(delimiter, offset):
                current = text[offset]
                if current == "\\" and offset + 1 < len(text):
                    offset += 1
                    column += 1
                    current = text[offset]
                literal.append(current)
                offset += 1
                if current == "\n":
                    line += 1
                    column = 1
                else:
                    column += 1
            if offset < len(text):
                offset += len(delimiter)
                column += len(delimiter)
            tokens.append(
                ToolToken("".join(literal), "string", start_line, start_column)
            )
            continue
        tokens.append(ToolToken(char, "punctuation", start_line, start_column))
        offset += 1
        column += 1
    return tuple(tokens)


def deterministic_tool_token_count(text: str) -> int:
    """Count exactly the tokens consumed by the restricted runtime lexer."""

    return len(deterministic_tool_tokens(text))


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


class ToolExecutionState(StrEnum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED_KNOWN = "failed_known"
    INTERRUPTED = "interrupted"
    DETERMINISM_VIOLATION = "determinism_violation"


@dataclass(frozen=True, slots=True)
class ToolLimits:
    max_input_bytes: int
    max_tokens: int
    max_rules: int
    max_steps: int
    max_matches: int
    max_output_items: int
    max_field_length: int
    soft_time_ms: int

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_field_length < SHA256_HEX_LENGTH:
            raise ValueError("max_field_length must be at least 64")

    def constrained_by(self, other: ToolLimits) -> ToolLimits:
        return ToolLimits(
            **{
                name: min(getattr(self, name), getattr(other, name))
                for name in self.__dataclass_fields__
            }
        )

    def digest_payload(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in sorted(self.__dataclass_fields__)}


@dataclass(frozen=True, slots=True)
class DeclarativeRule:
    rule_id: str
    op: str
    message: str
    params: MappingProxyType[str, RuleParameter]

    def digest_payload(self) -> dict[str, object]:
        return {
            "id": self.rule_id,
            "op": self.op,
            "message": self.message,
            "params": dict(self.params),
        }


@dataclass(frozen=True, slots=True)
class FixedToolReference:
    tool_id: str
    version: str
    contract_version: str
    origin: str
    resource_digest: str
    schema_digest: str
    declaration_digest: str


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    kind: str
    tool_id: str
    version: str
    contract_version: str
    display_name: str
    capabilities: tuple[str, ...]
    required_system_capabilities: tuple[str, ...]
    inputs: tuple[str, ...]
    languages: tuple[str, ...]
    deterministic: bool
    failure_impact: str
    origin: str
    rule_format_version: int
    resource_digest: str
    schema_digest: str
    limits: ToolLimits
    rules: tuple[DeclarativeRule, ...]
    declaration_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "declaration_digest", sha256_digest(self.digest_payload())
        )

    def digest_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.tool_id,
            "version": self.version,
            "contract_version": self.contract_version,
            "display_name": self.display_name,
            "capabilities": list(self.capabilities),
            "required_system_capabilities": list(self.required_system_capabilities),
            "inputs": list(self.inputs),
            "languages": list(self.languages),
            "deterministic": self.deterministic,
            "failure_impact": self.failure_impact,
            "origin": self.origin,
            "rule_format_version": self.rule_format_version,
            "resource_digest": self.resource_digest,
            "schema_digest": self.schema_digest,
            "limits": self.limits.digest_payload(),
            "rules": [rule.digest_payload() for rule in self.rules],
        }

    def fixed_reference(self) -> FixedToolReference:
        return FixedToolReference(
            tool_id=self.tool_id,
            version=self.version,
            contract_version=self.contract_version,
            origin=self.origin,
            resource_digest=self.resource_digest,
            schema_digest=self.schema_digest,
            declaration_digest=self.declaration_digest,
        )


@dataclass(frozen=True, slots=True)
class ToolRegistrySnapshot:
    tools: tuple[FixedToolReference, ...]
    catalog_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "catalog_digest",
            sha256_digest(
                [
                    {
                        "tool_id": item.tool_id,
                        "version": item.version,
                        "declaration_digest": item.declaration_digest,
                    }
                    for item in self.tools
                ]
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthorizedToolInput:
    text: str
    path: str
    scope: str
    token_count: int
    changed_lines: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if not self.path or not self.scope:
            raise ValueError("path and scope must be non-empty")
        if type(self.token_count) is not int or self.token_count < 0:
            raise ValueError("token_count must be a non-negative integer")
        if any(type(line) is not int or line <= 0 for line in self.changed_lines):
            raise ValueError("changed_lines must contain positive line numbers")
        if tuple(sorted(set(self.changed_lines))) != self.changed_lines:
            raise ValueError("changed_lines must be sorted and unique")

    @property
    def input_digest(self) -> str:
        return sha256_digest(
            {
                "text": self.text,
                "path": self.path,
                "scope": self.scope,
                "token_count": deterministic_tool_token_count(self.text),
                "changed_lines": list(self.changed_lines),
            }
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    task_id: str
    execution_id: str
    expected_success_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.task_id or not self.execution_id:
            raise ValueError("execution context identifiers must be non-empty")
        if self.expected_success_digest is not None and not _is_sha256(
            self.expected_success_digest
        ):
            raise ValueError("expected_success_digest must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    tool_id: str
    tool_version: str
    rule_id: str
    interpreter_version: str
    input_digest: str
    path: str
    scope: str
    line: int
    column: int
    op: str
    op_params_digest: str
    match_digest: str
    steps: int
    ordinal: int
    message: str


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    state: ToolExecutionState
    tool_id: str
    tool_version: str
    interpreter_version: str
    input_digest: str
    limits_digest: str
    steps: int
    evidence: tuple[ToolEvidence, ...]
    result_digest: str
    error_code: str | None = None


class ToolCatalogPort(Protocol):
    def freeze(self) -> ToolRegistrySnapshot: ...

    def catalog(self) -> tuple[ToolDeclaration, ...]: ...

    def resolve_exact(self, tool_id: str, version: str) -> ToolDeclaration: ...

    def verify_fixed_reference(
        self, reference: FixedToolReference
    ) -> ToolDeclaration: ...

    def disable_version(
        self, reference: FixedToolReference, *, reason: str
    ) -> None: ...


class RestrictedToolRuntimePort(Protocol):
    def execute(
        self,
        fixed_tool_ref: FixedToolReference,
        authorized_input: AuthorizedToolInput,
        planned_limits: ToolLimits,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult: ...
