from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from code_review_agent.application.dto import StartReviewCommand
from code_review_agent.bootstrap import ConfiguredRuntime
from code_review_agent.config import CliConfig


@respx.mock
def test_configured_runtime_completes_review_with_openai_provider(
    tmp_path: Path,
) -> None:
    config = CliConfig(
        provider_id="openai-compatible",
        provider_version="1",
        model_id="review-model",
        provider_origin="https://api.example.com",
        provider_path="/v1/chat/completions",
        api_key="secret-token",
        timeout_seconds=30,
        max_response_bytes=1_048_576,
        max_output_tokens=256,
    )
    route = respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"findings": []})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    command = StartReviewCommand(
        task_id="task-http-1",
        diff_text=(
            "diff --git a/demo.py b/demo.py\n"
            "--- a/demo.py\n"
            "+++ b/demo.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        diff_file=None,
        output_path=tmp_path / "reports" / "task-http-1.md",
    )

    result = ConfiguredRuntime(config).review(
        command,
        "openai-compatible",
        "review-model",
        50_000,
        "request-http-1",
    )

    assert route.called
    assert result.result_state == "complete_no_findings"
    assert result.delivery_state == "succeeded"
    assert command.output_path.exists()


@respx.mock
def test_configured_runtime_does_not_report_provider_failure_as_no_findings(
    tmp_path: Path,
) -> None:
    config = CliConfig(
        provider_id="openai-compatible",
        provider_version="1",
        model_id="review-model",
        provider_origin="https://api.example.com",
        provider_path="/v1/chat/completions",
        api_key="secret-token",
        timeout_seconds=30,
        max_response_bytes=1_048_576,
        max_output_tokens=256,
    )
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "invalid_api_key"})
    )
    command = StartReviewCommand(
        task_id="task-http-401",
        diff_text=(
            "diff --git a/demo.py b/demo.py\n"
            "--- a/demo.py\n"
            "+++ b/demo.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        diff_file=None,
        output_path=tmp_path / "reports" / "task-http-401.md",
    )

    result = ConfiguredRuntime(config).review(
        command,
        "openai-compatible",
        "review-model",
        50_000,
        "request-http-401",
    )

    assert result.result_state == "partial"
    assert result.delivery_state == "succeeded"
    report = command.output_path.read_text(encoding="utf-8")
    assert "审查部分完成" in report
    assert "provider_http_401" in report
    assert "未发现有效问题" not in report
