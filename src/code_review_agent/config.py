"""Configuration values owned by the CLI composition root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CliConfig:
    reports_dir: Path = Path("reports")
    default_budget_tokens: int = 50_000
    max_budget_tokens: int = 1_000_000
    provider_id: str = "local"
    provider_version: str = "1"
    model_id: str = "deterministic"
    provider_origin: str = "https://local.invalid"
    max_output_tokens: int = 256
    ruleset_id: str = "default"
    ruleset_version: str = "1"
    security_policy_id: str = "code-review-agent-default"
    security_policy_version: int = 1

    @classmethod
    def from_file(cls, path: Path) -> CliConfig:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("config_unreadable") from exc
        output = data.get("output", {})
        provider = data.get("provider", {})
        budget = data.get("budget", {})
        rules = data.get("rules", {})
        security = data.get("security", {})
        result = cls(
            reports_dir=Path(output.get("reports_dir", "reports")),
            default_budget_tokens=budget.get("default_tokens", 50_000),
            max_budget_tokens=budget.get("max_tokens", 1_000_000),
            provider_id=provider.get("id", "local"),
            provider_version=provider.get("version", "1"),
            model_id=provider.get("model", "deterministic"),
            provider_origin=provider.get("origin", "https://local.invalid"),
            max_output_tokens=provider.get("max_output_tokens", 256),
            ruleset_id=rules.get("id", "default"),
            ruleset_version=rules.get("version", "1"),
            security_policy_id=security.get(
                "policy_id", "code-review-agent-default"
            ),
            security_policy_version=security.get("policy_version", 1),
        )
        if not 1 <= result.default_budget_tokens <= result.max_budget_tokens:
            raise ValueError("config_invalid_budget")
        if result.max_output_tokens <= 0:
            raise ValueError("config_invalid_model")
        return result
