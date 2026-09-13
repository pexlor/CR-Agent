from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import httpx
import respx

from code_review_agent.application.dto import StartReviewCommand
from code_review_agent.application.persistence import ReviewStateStore
from code_review_agent.bootstrap import ConfiguredRuntime
from code_review_agent.config import CliConfig
from code_review_agent.domain.execution.execution_models import WorkUnitExecutionResult


@respx.mock
def test_configured_runtime_completes_review_with_openai_provider(
    tmp_path: Path,
) -> None:
    config = CliConfig(
        state_database=tmp_path / "state-http-1.sqlite3",
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
        "request-http-1",
    )

    assert route.called
    assert result.result_state == "complete_no_findings"
    assert result.delivery_state == "succeeded"
    assert command.output_path.exists()
    report = command.output_path.read_text(encoding="utf-8")
    assert "| Authorized | 500000 token |" in report
    assert "| Configured cost limit | 10.00 CNY |" in report
    assert "| Price per million tokens | 20.00 CNY |" in report
    _, _, _, stored_budget_tokens = ReviewStateStore(
        config.state_database
    ).command(command.task_id)
    assert stored_budget_tokens == 500_000


@respx.mock
def test_configured_runtime_does_not_report_provider_failure_as_no_findings(
    tmp_path: Path,
) -> None:
    config = CliConfig(
        state_database=tmp_path / "state-http-401.sqlite3",
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
        "request-http-401",
    )

    assert result.result_state == "partial"
    assert result.delivery_state == "succeeded"
    report = command.output_path.read_text(encoding="utf-8")
    assert "审查部分完成" in report
    assert "provider_http_401" in report
    assert "未发现有效问题" not in report


@respx.mock
def test_configured_runtime_delivers_non_empty_model_finding(
    tmp_path: Path,
) -> None:
    config = CliConfig(
        state_database=tmp_path / "state-http-finding.sqlite3",
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
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "findings": [
                                        {
                                            "category": "correctness",
                                            "title": "Wrong return shape",
                                            "problem": "The loader returns a list.",
                                            "trigger_condition": (
                                                "When a caller requests one user."
                                            ),
                                            "impact": "Authentication can fail.",
                                            "suggestion": "Return the matching user.",
                                            "change_causation": (
                                                "The changed line calls fetch_all."
                                            ),
                                            "limitations": "",
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 30},
            },
        )
    )
    command = StartReviewCommand(
        task_id="task-http-finding",
        diff_text=(
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-return db.fetch(user_id)\n"
            "+return db.fetch_all(user_id)\n"
        ),
        diff_file=None,
        output_path=tmp_path / "reports" / "finding.md",
    )

    runtime = ConfiguredRuntime(config)
    result = runtime.review(
        command,
        "openai-compatible",
        "review-model",
        "request-http-finding",
    )

    assert route.called
    request_body = json.loads(route.calls.last.request.content)
    system_message = request_body["messages"][0]["content"]
    assert "Output JSON Schema (follow exactly)" in system_message
    assert '"trigger_condition"' in system_message
    assert result.result_state == "complete_with_findings"
    report = command.output_path.read_text(encoding="utf-8")
    assert "Wrong return shape" in report
    assert "The loader returns a list." in report
    assert "Authentication can fail." in report
    assert "Return the matching user." in report
    assert "Trace:" in report
    finding_trace_ids = re.findall(r"Trace: `([^`]+)`", report)
    assert len(finding_trace_ids) == 1
    finding_trace = runtime.trace(finding_trace_ids[0])
    assert any(event.event_type == "model.call_succeeded" for event in finding_trace)
    assert any(event.event_type == "model.response_accepted" for event in finding_trace)
    assert any(event.event_type == "finding.validated" for event in finding_trace)
    request_event = next(
        event for event in finding_trace if event.event_type == "model.call_succeeded"
    )
    response_event = next(
        event
        for event in finding_trace
        if event.event_type == "model.response_accepted"
    )
    assert request_event.artifact is not None
    assert request_event.artifact.purpose == "trace_model_request"
    assert response_event.artifact is not None
    assert response_event.artifact.purpose == "trace_model_response"

    with sqlite3.connect(config.state_database) as connection:
        binding = connection.execute(
            "SELECT input_digest, rules_config_digest FROM review_checkpoints "
            "WHERE task_id = ?",
            (command.task_id,),
        ).fetchone()
    checkpoint = ReviewStateStore(config.state_database).load_checkpoint(
        command.task_id,
        input_digest=str(binding[0]),
        rules_config_digest=str(binding[1]),
    )
    restored = next(iter(dict(checkpoint.completed_units).values()))
    assert isinstance(restored, WorkUnitExecutionResult)
    assert restored.tool_attempts
    assert restored.model_attempt is not None
    assert restored.model_attempt.outcome is not None
    assert restored.model_attempt.outcome.response_payload is not None
    assert restored.candidates
    assert restored.evidence
