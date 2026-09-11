from __future__ import annotations

from dataclasses import replace

import pytest
from code_review_agent.adapters.tools.registry import (
    ToolManifestError,
    ToolRegistry,
    ToolRegistryConflict,
    ToolRegistryFrozen,
)

from .conftest import tool_manifest

ALL_ALLOWED_RULES = (
    {
        "id": "literal",
        "op": "literal_contains",
        "message": "literal present",
        "params": {"literal": "eval("},
    },
    {
        "id": "missing",
        "op": "literal_not_contains",
        "message": "required text absent",
        "params": {"literal": "copyright"},
    },
    {
        "id": "tokens",
        "op": "token_sequence",
        "message": "token sequence present",
        "params": {"tokens": ["subprocess", ".", "run"]},
    },
    {
        "id": "nearby",
        "op": "token_pair_within",
        "message": "tokens too close",
        "params": {"first": "password", "second": "log", "max_tokens": 4},
    },
    {
        "id": "line-predicate",
        "op": "line_predicate",
        "message": "line predicate matched",
        "params": {
            "equals": ["DEBUG"],
            "prefixes": ["TODO:"],
            "suffixes": [" unsafe"],
        },
    },
    {
        "id": "prefix",
        "op": "line_prefix",
        "message": "prefix present",
        "params": {"literal": "TODO:"},
    },
    {
        "id": "suffix",
        "op": "line_suffix",
        "message": "suffix present",
        "params": {"literal": ";"},
    },
    {
        "id": "equals",
        "op": "line_equals",
        "message": "line equals literal",
        "params": {"literal": "DEBUG"},
    },
    {
        "id": "delimiter",
        "op": "balanced_delimiter",
        "message": "delimiter imbalance",
        "params": {"open": "(", "close": ")"},
    },
    {
        "id": "changed",
        "op": "changed_line_only",
        "message": "changed line matched",
        "params": {"literal": "danger"},
    },
    {
        "id": "context",
        "op": "bounded_context_contains",
        "message": "nearby context matched",
        "params": {"anchor": "auth", "literal": "disable", "max_lines": 1},
    },
    {
        "id": "identifier",
        "op": "identifier_equals",
        "message": "identifier matched",
        "params": {"identifier": "eval"},
    },
    {
        "id": "call",
        "op": "call_name_equals",
        "message": "call matched",
        "params": {"name": "eval"},
    },
    {
        "id": "argument",
        "op": "argument_literal_equals",
        "message": "literal argument matched",
        "params": {"call": "mode", "literal": "unsafe"},
    },
)


def test_builtin_manifest_loads_and_resolves_an_exact_frozen_reference() -> None:
    registry = ToolRegistry.from_builtin()

    snapshot = registry.freeze()
    declaration = registry.resolve_exact("dangerous-python-execution", "1.0.0")
    reference = declaration.fixed_reference()

    assert snapshot.tools == (reference,)
    assert registry.verify_fixed_reference(reference) is declaration
    with pytest.raises(ToolRegistryFrozen):
        registry.register_toml(tool_manifest(tool_id="late-tool"))


def test_registry_accepts_only_the_documented_finite_operations() -> None:
    registry = ToolRegistry()

    registered = registry.register_toml(tool_manifest(rules=ALL_ALLOWED_RULES))

    assert tuple(rule.op for rule in registered[0].rules) == tuple(
        rule["op"] for rule in ALL_ALLOWED_RULES
    )


@pytest.mark.parametrize(
    "extra_field",
    (
        'unexpected = "value"',
        'url = "https://example.invalid/rules"',
        'entry_point = "package.module:tool"',
        'callback = "package.module:callback"',
        'shell = "sh -c whoami"',
        'glob = "**/*.py"',
        'regex = ".*"',
        'environment = "HOME"',
        'repository_path = "./tool.py"',
        'python_import = "package.module"',
    ),
)
def test_registry_rejects_unknown_or_privileged_declaration_fields(
    extra_field: str,
) -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolManifestError) as raised:
        registry.register_toml(tool_manifest(extra_tool_fields=extra_field))

    assert raised.value.code == "tool_manifest_unknown_field"


def test_registry_rejects_unknown_operations_and_dynamic_rule_parameters() -> None:
    unknown_op = (
        {
            "id": "regex-rule",
            "op": "regex",
            "message": "must never run",
            "params": {"pattern": ".*"},
        },
    )
    dynamic_predicate = (
        {
            "id": "predicate-rule",
            "op": "line_predicate",
            "message": "must never evaluate",
            "params": {"expression": "__import__('os').system('whoami')"},
        },
    )

    for rules in (unknown_op, dynamic_predicate):
        with pytest.raises(ToolManifestError):
            ToolRegistry().register_toml(tool_manifest(rules=rules))


def test_registry_rejects_duplicate_identity_without_overwriting() -> None:
    registry = ToolRegistry()
    original = registry.register_toml(tool_manifest())[0]

    with pytest.raises(ToolRegistryConflict):
        registry.register_toml(tool_manifest())

    assert registry.resolve_exact(original.tool_id, original.version) is original


def test_fixed_reference_detects_any_manifest_drift() -> None:
    registry = ToolRegistry()
    declaration = registry.register_toml(tool_manifest())[0]
    registry.freeze()

    stale = replace(declaration.fixed_reference(), resource_digest="0" * 64)

    with pytest.raises(ToolManifestError) as raised:
        registry.verify_fixed_reference(stale)
    assert raised.value.code == "tool_reference_mismatch"
