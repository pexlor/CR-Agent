"""Configuration values owned by the CLI composition root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from stat import S_IRWXG, S_IRWXO
from urllib.parse import urlparse

_MAX_DECIMAL_PLACES = 6
_MAX_AUTHORIZATION_TOKENS = 1_000_000


@dataclass(frozen=True, slots=True)
class CliConfig:
    reports_dir: Path = Path("reports")
    state_database: Path = Path("state/reviews.sqlite3")
    max_cost_per_review_cny: Decimal = Decimal("10.00")
    price_per_million_tokens_cny: Decimal = Decimal("20.00")
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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_cost_per_review_cny",
            _positive_decimal(
                self.max_cost_per_review_cny,
                "config_invalid_cost_budget",
                max_decimal_places=_MAX_DECIMAL_PLACES,
            ),
        )
        object.__setattr__(
            self,
            "price_per_million_tokens_cny",
            _positive_decimal(
                self.price_per_million_tokens_cny,
                "config_invalid_model_price",
                max_decimal_places=_MAX_DECIMAL_PLACES,
            ),
        )
        if (
            type(self.max_budget_tokens) is not int
            or not 1 <= self.max_budget_tokens <= _MAX_AUTHORIZATION_TOKENS
        ):
            raise ValueError("config_invalid_budget")
        if self.budget_tokens_per_review < 1:
            raise ValueError("config_cost_budget_too_small")
        if self.budget_tokens_per_review > self.max_budget_tokens:
            raise ValueError("config_cost_budget_exceeds_token_limit")

    @property
    def budget_tokens_per_review(self) -> int:
        value = (
            self.max_cost_per_review_cny
            * Decimal(1_000_000)
            / self.price_per_million_tokens_cny
        )
        return int(value.to_integral_value(rounding=ROUND_FLOOR))

    def cost_cny_for_tokens(self, tokens: int) -> Decimal:
        if type(tokens) is not int or tokens < 0:
            raise ValueError("token amount must be a non-negative integer")
        return (
            Decimal(tokens)
            * self.price_per_million_tokens_cny
            / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"))

    def format_cost_cny_for_tokens(self, tokens: int) -> str:
        """Render the exact (unrounded) CNY cost, preserving nonzero fractions."""
        if type(tokens) is not int or tokens < 0:
            raise ValueError("token amount must be a non-negative integer")
        exact = (
            Decimal(tokens) * self.price_per_million_tokens_cny / Decimal(1_000_000)
        )
        return format(exact.normalize(), "f")

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
        if "max_cost_per_review_cny" not in budget:
            raise ValueError("config_missing_cost_budget")
        if "price_per_million_tokens_cny" not in budget:
            raise ValueError("config_missing_model_price")
        result = cls(
            reports_dir=Path(output.get("reports_dir", "reports")),
            state_database=Path(output.get("state_database", "state/reviews.sqlite3")),
            max_cost_per_review_cny=budget["max_cost_per_review_cny"],
            price_per_million_tokens_cny=budget["price_per_million_tokens_cny"],
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


def _positive_decimal(
    value: object, error_code: str, *, max_decimal_places: int
) -> Decimal:
    # Floats are rejected outright: TOML bare numeric literals (e.g. `20.000000000000001`)
    # lose precision to IEEE754 binary rounding before they ever reach this function, which
    # would silently defeat the decimal-place check below. Prices must be quoted strings
    # (or plain ints) so the exact decimal digits survive intact.
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError(error_code)
    try:
        parsed = Decimal(value) if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(error_code) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(error_code)
    exponent = parsed.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > max_decimal_places:
        raise ValueError(error_code)
    return parsed
