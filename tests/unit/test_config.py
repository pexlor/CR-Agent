from __future__ import annotations

from pathlib import Path

import pytest

from code_review_agent.config import CliConfig


def _write_config(path: Path, provider: str) -> None:
    path.write_text(
        f"""
[output]
reports_dir = "reports"

[budget]
default_tokens = 50000
max_tokens = 1000000

[provider]
id = "{provider}"
version = "1"
model = "review-model"
origin = "https://api.example.com"
path = "/v1/chat/completions"
api_key = "secret-token"
timeout_seconds = 30
max_response_bytes = 1048576
max_output_tokens = 512
structured_output = "json_object"

[rules]
id = "default"
version = "1"

[security]
policy_id = "code-review-agent-default"
policy_version = 1
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_openai_compatible_config_loads_provider_fields(tmp_path: Path) -> None:
    path = tmp_path / "agent.toml"
    _write_config(path, "openai-compatible")
    path.chmod(0o600)

    config = CliConfig.from_file(path)

    assert config.provider_path == "/v1/chat/completions"
    assert config.api_key == "secret-token"
    assert config.timeout_seconds == 30
    assert config.max_response_bytes == 1_048_576
    assert config.structured_output == "json_object"


def test_inline_token_config_rejects_group_or_other_read_access(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent.toml"
    _write_config(path, "openai-compatible")
    path.chmod(0o644)

    with pytest.raises(ValueError, match="config_permissions_too_open"):
        CliConfig.from_file(path)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("origin", '"http://api.example.com"', "config_invalid_origin"),
        ("path", '"v1/chat/completions"', "config_invalid_path"),
        ("timeout_seconds", "0", "config_invalid_timeout"),
        ("max_response_bytes", "0", "config_invalid_response_limit"),
    ],
)
def test_openai_compatible_config_rejects_invalid_transport_fields(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    path = tmp_path / "agent.toml"
    _write_config(path, "openai-compatible")
    content = path.read_text(encoding="utf-8")
    old_line = next(line for line in content.splitlines() if line.startswith(field))
    path.write_text(content.replace(old_line, f"{field} = {value}"), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ValueError, match=code):
        CliConfig.from_file(path)

