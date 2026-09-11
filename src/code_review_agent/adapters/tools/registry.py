"""Strict registry for locked, declarative review-tool resources."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from importlib import resources
from types import MappingProxyType
from typing import Any

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.common.errors import SafeScalar, StableError
from code_review_agent.ports.tools import (
    DeclarativeRule,
    FixedToolReference,
    RuleParameter,
    ToolDeclaration,
    ToolLimits,
    ToolRegistrySnapshot,
)

INTERPRETER_CONTRACT_MAJOR = 1
MAX_VERSION_LENGTH = 64
MAX_TOOL_ID_LENGTH = 64
TOOL_SCHEMA_DIGEST_V1 = sha256_digest(
    {"contract_version": "1.0", "rule_format_version": 1}
)

_TOP_LEVEL_FIELDS = frozenset({"manifest_version", "tools"})
_TOOL_FIELDS = frozenset(
    {
        "kind",
        "id",
        "version",
        "contract_version",
        "display_name",
        "capabilities",
        "required_system_capabilities",
        "inputs",
        "languages",
        "deterministic",
        "failure_impact",
        "origin",
        "rule_format_version",
        "resource_digest",
        "schema_digest",
        "limits",
        "rules",
    }
)
_LIMIT_FIELDS = frozenset(ToolLimits.__dataclass_fields__)
_RULE_FIELDS = frozenset({"id", "op", "message", "params"})

_PARAM_SCHEMAS: dict[str, dict[str, type[object]]] = {
    "literal_contains": {"literal": str},
    "literal_not_contains": {"literal": str},
    "token_sequence": {"tokens": list},
    "token_pair_within": {"first": str, "second": str, "max_tokens": int},
    "line_predicate": {"equals": list, "prefixes": list, "suffixes": list},
    "line_prefix": {"literal": str},
    "line_suffix": {"literal": str},
    "line_equals": {"literal": str},
    "balanced_delimiter": {"open": str, "close": str},
    "changed_line_only": {"literal": str},
    "bounded_context_contains": {"anchor": str, "literal": str, "max_lines": int},
    "identifier_equals": {"identifier": str},
    "call_name_equals": {"name": str},
    "argument_literal_equals": {"call": str, "literal": str},
}


class ToolRegistryError(StableError):
    def __init__(self, code: str, *, details: Mapping[str, SafeScalar] | None = None):
        super().__init__(
            code=code,
            category="tool_registry",
            stage="tool_registration",
            recoverable=False,
            details=details,
        )


class ToolManifestError(ToolRegistryError):
    pass


class ToolRegistryConflict(ToolRegistryError):
    pass


class ToolRegistryFrozen(ToolRegistryError):
    pass


class ToolVersionDisabled(ToolRegistryError):
    pass


def _is_stable_id(value: str) -> bool:
    parts = value.split("-")
    return bool(parts) and all(
        part
        and part[0].islower()
        and part[0].isascii()
        and all(char.isascii() and (char.islower() or char.isdigit()) for char in part)
        for part in parts
    )


def _numeric_version_parts(value: str, *, count: int) -> tuple[int, ...] | None:
    if len(value) > MAX_VERSION_LENGTH:
        return None
    parts = value.split(".")
    if len(parts) != count:
        return None
    if any(
        not part.isascii()
        or not part.isdigit()
        or (len(part) > 1 and part.startswith("0"))
        for part in parts
    ):
        return None
    return tuple(int(part) for part in parts)


def _require_exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], *, location: str
) -> None:
    unknown = set(value) - expected
    if unknown:
        raise ToolManifestError(
            "tool_manifest_unknown_field", details={"location": location}
        )
    missing = expected - set(value)
    if missing:
        raise ToolManifestError(
            "tool_manifest_missing_field", details={"location": location}
        )


def _require_string(value: Any, *, field: str, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ToolManifestError("tool_manifest_invalid_field", details={"field": field})
    if max_length is not None and len(value) > max_length:
        raise ToolManifestError(
            "tool_manifest_field_too_long", details={"field": field}
        )
    return value


def _require_string_tuple(
    value: Any, *, field: str, allow_empty: bool, max_length: int | None = None
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ToolManifestError("tool_manifest_invalid_field", details={"field": field})
    result = tuple(
        _require_string(item, field=field, max_length=max_length) for item in value
    )
    if len(set(result)) != len(result):
        raise ToolManifestError(
            "tool_manifest_duplicate_value", details={"field": field}
        )
    return result


def _parse_limits(value: Any) -> ToolLimits:
    if not isinstance(value, dict):
        raise ToolManifestError("tool_manifest_invalid_limits")
    _require_exact_fields(value, _LIMIT_FIELDS, location="limits")
    if any(type(item) is not int or item <= 0 for item in value.values()):
        raise ToolManifestError("tool_manifest_invalid_limits")
    return ToolLimits(**value)


def _parse_params(
    op: str, value: Any, *, max_field_length: int
) -> MappingProxyType[str, RuleParameter]:
    if op not in _PARAM_SCHEMAS:
        raise ToolManifestError("tool_manifest_unknown_operation")
    if not isinstance(value, dict):
        raise ToolManifestError("tool_manifest_invalid_parameters")
    schema = _PARAM_SCHEMAS[op]
    if op == "line_predicate":
        unknown = set(value) - set(schema)
        if unknown or not value:
            raise ToolManifestError("tool_manifest_invalid_parameters")
    elif set(value) != set(schema):
        raise ToolManifestError("tool_manifest_invalid_parameters")

    normalized: dict[str, RuleParameter] = {}
    for name, item in value.items():
        expected = schema[name]
        if expected is str:
            normalized[name] = _require_string(
                item, field=f"params.{name}", max_length=max_field_length
            )
        elif expected is int:
            if type(item) is not int or item < 0:
                raise ToolManifestError("tool_manifest_invalid_parameters")
            normalized[name] = item
        else:
            normalized[name] = _require_string_tuple(
                item,
                field=f"params.{name}",
                allow_empty=False,
                max_length=max_field_length,
            )

    if op == "balanced_delimiter" and normalized["open"] == normalized["close"]:
        raise ToolManifestError("tool_manifest_invalid_parameters")
    return MappingProxyType(normalized)


def _parse_rule(value: Any, *, limits: ToolLimits) -> DeclarativeRule:
    if not isinstance(value, dict):
        raise ToolManifestError("tool_manifest_invalid_rule")
    _require_exact_fields(value, _RULE_FIELDS, location="rule")
    rule_id = _require_string(
        value["id"], field="rule.id", max_length=limits.max_field_length
    )
    if not _is_stable_id(rule_id):
        raise ToolManifestError("tool_manifest_invalid_rule_id")
    op = _require_string(value["op"], field="rule.op")
    message = _require_string(
        value["message"], field="rule.message", max_length=limits.max_field_length
    )
    return DeclarativeRule(
        rule_id=rule_id,
        op=op,
        message=message,
        params=_parse_params(
            op, value["params"], max_field_length=limits.max_field_length
        ),
    )


def _parse_tool(value: Any) -> ToolDeclaration:
    if not isinstance(value, dict):
        raise ToolManifestError("tool_manifest_invalid_tool")
    _require_exact_fields(value, _TOOL_FIELDS, location="tool")
    limits = _parse_limits(value["limits"])
    tool_id = _require_string(value["id"], field="id", max_length=MAX_TOOL_ID_LENGTH)
    version = _require_string(value["version"], field="version")
    contract_version = _require_string(
        value["contract_version"], field="contract_version"
    )
    if not _is_stable_id(tool_id) or _numeric_version_parts(version, count=3) is None:
        raise ToolManifestError("tool_manifest_invalid_identity")
    contract_parts = _numeric_version_parts(contract_version, count=2)
    if contract_parts is None or contract_parts[0] != INTERPRETER_CONTRACT_MAJOR:
        raise ToolManifestError("tool_manifest_incompatible_contract")
    if value["kind"] != "review_tool":
        raise ToolManifestError("tool_manifest_invalid_kind")
    if value["deterministic"] is not True:
        raise ToolManifestError("tool_manifest_nondeterministic")

    capabilities = _require_string_tuple(
        value["capabilities"], field="capabilities", allow_empty=False
    )
    required_capabilities = _require_string_tuple(
        value["required_system_capabilities"],
        field="required_system_capabilities",
        allow_empty=True,
    )
    inputs = _require_string_tuple(value["inputs"], field="inputs", allow_empty=False)
    languages = _require_string_tuple(
        value["languages"], field="languages", allow_empty=False
    )
    if capabilities != ("deterministic_text_analysis",):
        raise ToolManifestError("tool_manifest_invalid_capabilities")
    if required_capabilities:
        raise ToolManifestError("tool_manifest_privileged_capability")
    if inputs != ("authorized_text",):
        raise ToolManifestError("tool_manifest_invalid_input")
    if value["failure_impact"] != "partial_validation":
        raise ToolManifestError("tool_manifest_invalid_failure_impact")
    if value["origin"] not in ("builtin", "locked_release"):
        raise ToolManifestError("tool_manifest_invalid_origin")
    if value["rule_format_version"] != 1:
        raise ToolManifestError("tool_manifest_incompatible_rule_format")
    if value["schema_digest"] != TOOL_SCHEMA_DIGEST_V1:
        raise ToolManifestError("tool_manifest_schema_digest_mismatch")

    raw_rules = value["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ToolManifestError("tool_manifest_invalid_rules")
    if len(raw_rules) > limits.max_rules:
        raise ToolManifestError("tool_manifest_rule_limit_exceeded")
    rules = tuple(_parse_rule(rule, limits=limits) for rule in raw_rules)
    if len({rule.rule_id for rule in rules}) != len(rules):
        raise ToolManifestError("tool_manifest_duplicate_rule")
    expected_resource_digest = sha256_digest([rule.digest_payload() for rule in rules])
    if value["resource_digest"] != expected_resource_digest:
        raise ToolManifestError("tool_manifest_resource_digest_mismatch")

    return ToolDeclaration(
        kind="review_tool",
        tool_id=tool_id,
        version=version,
        contract_version=contract_version,
        display_name=_require_string(
            value["display_name"],
            field="display_name",
            max_length=limits.max_field_length,
        ),
        capabilities=capabilities,
        required_system_capabilities=required_capabilities,
        inputs=inputs,
        languages=languages,
        deterministic=True,
        failure_impact="partial_validation",
        origin=value["origin"],
        rule_format_version=1,
        resource_digest=value["resource_digest"],
        schema_digest=value["schema_digest"],
        limits=limits,
        rules=rules,
    )


def _decode_manifest(manifest: bytes) -> dict[str, Any] | None:
    try:
        return tomllib.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _normalize_tools(raw_tools: list[Any]) -> tuple[ToolDeclaration, ...] | None:
    try:
        return tuple(_parse_tool(tool) for tool in raw_tools)
    except ToolRegistryError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class ToolRegistry:
    """Registers validated declarative data and freezes exact tool identities."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], ToolDeclaration] = {}
        self._disabled: dict[tuple[str, str], str] = {}
        self._snapshot: ToolRegistrySnapshot | None = None

    @classmethod
    def from_builtin(cls) -> ToolRegistry:
        registry = cls()
        manifest = (
            resources.files("code_review_agent.resources.tools")
            .joinpath("builtin.toml")
            .read_bytes()
        )
        registry.register_toml(manifest)
        return registry

    def register_toml(self, manifest: bytes) -> tuple[ToolDeclaration, ...]:
        if self._snapshot is not None:
            raise ToolRegistryFrozen("tool_registry_frozen")
        if not isinstance(manifest, bytes):
            raise TypeError("manifest must be locked bytes")
        document = _decode_manifest(manifest)
        if document is None:
            raise ToolManifestError("tool_manifest_invalid_toml")
        _require_exact_fields(document, _TOP_LEVEL_FIELDS, location="manifest")
        if document["manifest_version"] != 1:
            raise ToolManifestError("tool_manifest_incompatible_version")
        raw_tools = document["tools"]
        if not isinstance(raw_tools, list) or not raw_tools:
            raise ToolManifestError("tool_manifest_invalid_tools")

        parsed = _normalize_tools(raw_tools)
        if parsed is None:
            raise ToolManifestError("tool_manifest_invalid_field")
        keys = tuple((tool.tool_id, tool.version) for tool in parsed)
        if len(set(keys)) != len(keys) or any(key in self._tools for key in keys):
            raise ToolRegistryConflict("tool_registry_identity_conflict")
        self._tools.update(zip(keys, parsed, strict=True))
        return parsed

    def catalog(self) -> tuple[ToolDeclaration, ...]:
        return tuple(
            self._tools[key] for key in sorted(self._tools) if key not in self._disabled
        )

    def freeze(self) -> ToolRegistrySnapshot:
        if self._snapshot is None:
            self._snapshot = ToolRegistrySnapshot(
                tuple(tool.fixed_reference() for tool in self.catalog())
            )
        return self._snapshot

    def resolve_exact(self, tool_id: str, version: str) -> ToolDeclaration:
        key = (tool_id, version)
        declaration = self._tools.get(key)
        if declaration is None:
            raise ToolManifestError(
                "tool_not_registered", details={"tool_id": tool_id, "version": version}
            )
        if key in self._disabled:
            raise ToolVersionDisabled(
                "tool_version_disabled",
                details={"tool_id": tool_id, "version": version},
            )
        return declaration

    def verify_fixed_reference(self, reference: FixedToolReference) -> ToolDeclaration:
        declaration = self.resolve_exact(reference.tool_id, reference.version)
        if declaration.fixed_reference() != reference:
            raise ToolManifestError(
                "tool_reference_mismatch",
                details={"tool_id": reference.tool_id, "version": reference.version},
            )
        return declaration

    def disable_version(self, reference: FixedToolReference, *, reason: str) -> None:
        key = (reference.tool_id, reference.version)
        declaration = self._tools.get(key)
        if declaration is None or declaration.fixed_reference() != reference:
            raise ToolManifestError(
                "tool_reference_mismatch",
                details={"tool_id": reference.tool_id, "version": reference.version},
            )
        if reason != "determinism_violation":
            raise ToolManifestError("tool_disable_reason_invalid")
        self._disabled[key] = reason
