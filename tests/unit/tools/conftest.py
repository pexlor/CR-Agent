from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from code_review_agent.domain.common.digests import sha256_digest

DEFAULT_LIMITS = {
    "max_input_bytes": 32_768,
    "max_tokens": 8_192,
    "max_rules": 64,
    "max_steps": 20_000,
    "max_matches": 256,
    "max_output_items": 256,
    "max_field_length": 512,
    "soft_time_ms": 1_000,
}


def _toml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def tool_manifest(
    *,
    tool_id: str = "test-patterns",
    version: str = "1.0.0",
    rules: Sequence[Mapping[str, Any]] | None = None,
    extra_tool_fields: str = "",
    limits: Mapping[str, int] = DEFAULT_LIMITS,
) -> bytes:
    normalized_rules = list(
        rules
        or (
            {
                "id": "dangerous-eval",
                "op": "literal_contains",
                "message": "Dynamic evaluation is dangerous",
                "params": {"literal": "eval("},
            },
        )
    )
    resource_digest = sha256_digest(normalized_rules)
    schema_digest = sha256_digest({"contract_version": "1.0", "rule_format_version": 1})
    lines = [
        "manifest_version = 1",
        "",
        "[[tools]]",
        'kind = "review_tool"',
        f"id = {_toml_value(tool_id)}",
        f"version = {_toml_value(version)}",
        'contract_version = "1.0"',
        f"display_name = {_toml_value(tool_id.replace('-', ' ').title())}",
        'capabilities = ["deterministic_text_analysis"]',
        "required_system_capabilities = []",
        'inputs = ["authorized_text"]',
        'languages = ["*"]',
        "deterministic = true",
        'failure_impact = "partial_validation"',
        'origin = "builtin"',
        "rule_format_version = 1",
        f"resource_digest = {_toml_value(resource_digest)}",
        f"schema_digest = {_toml_value(schema_digest)}",
    ]
    if extra_tool_fields:
        lines.append(extra_tool_fields)
    lines.extend(("", "[tools.limits]"))
    lines.extend(f"{key} = {value}" for key, value in limits.items())
    for rule in normalized_rules:
        lines.extend(
            (
                "",
                "[[tools.rules]]",
                f"id = {_toml_value(rule['id'])}",
                f"op = {_toml_value(rule['op'])}",
                f"message = {_toml_value(rule['message'])}",
                "[tools.rules.params]",
            )
        )
        params = rule["params"]
        assert isinstance(params, Mapping)
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in params.items())
    return ("\n".join(lines) + "\n").encode()
