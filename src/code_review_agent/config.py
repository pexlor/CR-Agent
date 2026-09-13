"""Configuration values owned by the CLI composition root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_IRWXG, S_IRWXO
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class CliConfig:
    reports_dir: Path = Path("reports")
    state_database: Path = Path("state/reviews.sqlite3")
    default_budget_tokens: int = 50_000
    max_budget_tokens: int = 1_000_000
    provider_id: str = "local"
    provider_version: str = "1"
    model_id: str = "deterministic"
    provider_origin: str = "https://local.invalid"
    provider_path: str = "/review"
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: int = 60
    max_response_bytes: int = 1_048_576
    max_output_tokens: int = 256
    structured_output: str = "json_object"
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
            state_database=Path(output.get("state_database", "state/reviews.sqlite3")),
            default_budget_tokens=budget.get("default_tokens", 50_000),
            max_budget_tokens=budget.get("max_tokens", 1_000_000),
            provider_id=provider.get("id", "local"),
            provider_version=provider.get("version", "1"),
            model_id=provider.get("model", "deterministic"),
            provider_origin=provider.get("origin", "https://local.invalid"),
            provider_path=provider.get("path", "/review"),
            api_key=provider.get("api_key"),
            timeout_seconds=provider.get("timeout_seconds", 60),
            max_response_bytes=provider.get("max_response_bytes", 1_048_576),
            max_output_tokens=provider.get("max_output_tokens", 256),
            structured_output=provider.get("structured_output", "json_object"),
            ruleset_id=rules.get("id", "default"),
            ruleset_version=rules.get("version", "1"),
            security_policy_id=security.get("policy_id", "code-review-agent-default"),
            security_policy_version=security.get("policy_version", 1),
        )
        if not 1 <= result.default_budget_tokens <= result.max_budget_tokens:
            raise ValueError("config_invalid_budget")
        if result.max_output_tokens <= 0:
            raise ValueError("config_invalid_model")
        if result.provider_id == "openai-compatible":
            cls._validate_openai_compatible(result, path)
        return result

    @staticmethod
    def _validate_openai_compatible(config: CliConfig, path: Path) -> None:
        parsed = urlparse(config.provider_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("config_invalid_origin")
        if (
            not config.provider_path.startswith("/")
            or config.provider_path.startswith("//")
            or "?" in config.provider_path
            or "#" in config.provider_path
        ):
            raise ValueError("config_invalid_path")
        if not config.api_key:
            raise ValueError("config_missing_api_key")
        if type(config.timeout_seconds) is not int or config.timeout_seconds <= 0:
            raise ValueError("config_invalid_timeout")
        if type(config.max_response_bytes) is not int or config.max_response_bytes <= 0:
            raise ValueError("config_invalid_response_limit")
        if config.structured_output != "json_object":
            raise ValueError("config_invalid_structured_output")
        if path.stat().st_mode & (S_IRWXG | S_IRWXO):
            raise ValueError("config_permissions_too_open")
