from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from code_review_agent.application.dto import ReviewProgressView, ReviewRunResult
from code_review_agent.cli.app import create_app


class FakeRuntime:
    def __init__(self) -> None:
        self.review_calls: list[tuple[object, str, str, int, str | None]] = []
        self.result = ReviewRunResult(
            task_id="task-1",
            session_id="session-1",
            phase="completed",
            result_state="complete_no_findings",
            delivery_state="succeeded",
            report_path=Path("reports/task-1.md"),
            report_digest="digest",
        )

    def review(
        self,
        command: object,
        provider: str,
        model: str,
        budget_tokens: int,
        request_id: str | None,
    ) -> ReviewRunResult:
        self.review_calls.append((command, provider, model, budget_tokens, request_id))
        return self.result

    def status(self, task_id: str) -> ReviewProgressView:
        return ReviewProgressView(
            task_id=task_id,
            phase="completed",
            result_state="complete_no_findings",
            delivery_state="succeeded",
            trace=(),
        )

    def trace(self, trace_id: str) -> tuple[object, ...]:
        return (SimpleNamespace(sequence=1, phase="completed", message=trace_id),)

    def resume(
        self, task_id: str, *, confirm_unknown_retry: bool = False
    ) -> ReviewRunResult:
        if task_id == "unknown" and not confirm_unknown_retry:
            raise ValueError("unknown_retry_confirmation_required")
        return self.result

    def terminate(
        self, task_id: str, *, reason: str, expected_version: int
    ) -> ReviewRunResult:
        assert task_id == "task-1"
        assert reason
        assert expected_version == 1
        return self.result

    def cleanup(self, task_id: str, *, expected_version: int) -> None:
        assert task_id == "task-1"
        assert expected_version == 1

    def retry_delivery(self, task_id: str, *, expected_version: int) -> ReviewRunResult:
        assert task_id == "task-1"
        assert expected_version == 1
        return self.result

    def providers(self) -> tuple[dict[str, str], ...]:
        return ({"kind": "input", "provider_id": "fake", "version": "1"},)


def test_review_requires_exactly_one_input_source() -> None:
    runtime = FakeRuntime()
    result = CliRunner().invoke(
        create_app(lambda: runtime),
        [
            "review",
            "--diff-file",
            "change.diff",
            "--stdin",
            "--provider",
            "fake",
            "--model",
            "fake-model",
        ],
    )

    assert result.exit_code == 2
    assert "exactly one" in result.stderr.lower()
    assert runtime.review_calls == []


def test_review_rejects_invalid_budget_without_calling_application() -> None:
    runtime = FakeRuntime()
    result = CliRunner().invoke(
        create_app(lambda: runtime),
        [
            "review",
            "--diff-file",
            "change.diff",
            "--provider",
            "fake",
            "--model",
            "fake-model",
            "--budget-tokens",
            "0",
        ],
    )

    assert result.exit_code == 2
    assert runtime.review_calls == []


def test_review_json_uses_final_result_on_stdout(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    result = CliRunner().invoke(
        create_app(lambda: runtime),
        [
            "review",
            "--diff-file",
            str(tmp_path / "change.diff"),
            "--provider",
            "fake",
            "--model",
            "fake-model",
            "--json",
            "--request-id",
            "request-1",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["task_id"] == "task-1"
    assert result.stderr == ""
    assert runtime.review_calls[0][3] == 50_000


def test_status_and_trace_show_use_shared_output_contract() -> None:
    runtime = FakeRuntime()
    runner = CliRunner()

    status = runner.invoke(
        create_app(lambda: runtime),
        ["status", "task-1", "--json"],
    )
    trace = runner.invoke(
        create_app(lambda: runtime),
        ["trace", "show", "task-1", "--json"],
    )

    assert status.exit_code == 0
    assert json.loads(status.stdout)["data"]["task_id"] == "task-1"
    assert trace.exit_code == 0
    assert json.loads(trace.stdout)["data"]["trace_id"] == "task-1"


def test_control_commands_require_confirmation_and_forward_versions() -> None:
    runtime = FakeRuntime()
    runner = CliRunner()

    missing_confirmation = runner.invoke(
        create_app(lambda: runtime),
        [
            "terminate",
            "task-1",
            "--reason",
            "operator request",
            "--expected-version",
            "1",
        ],
    )
    terminated = runner.invoke(
        create_app(lambda: runtime),
        [
            "terminate",
            "task-1",
            "--reason",
            "operator request",
            "--expected-version",
            "1",
            "--confirm",
            "--request-id",
            "req-1",
        ],
    )
    cleaned = runner.invoke(
        create_app(lambda: runtime),
        ["cleanup", "task-1", "--expected-version", "1", "--confirm"],
    )

    assert missing_confirmation.exit_code == 2
    assert terminated.exit_code == 0
    assert json.loads(terminated.stdout)["request_id"] == "req-1"
    assert cleaned.exit_code == 0


def test_resume_unknown_requires_explicit_confirmation() -> None:
    runtime = FakeRuntime()
    runner = CliRunner()

    blocked = runner.invoke(create_app(lambda: runtime), ["resume", "unknown"])
    retried = runner.invoke(
        create_app(lambda: runtime),
        ["resume", "unknown", "--confirm-unknown-retry", "--json"],
    )

    assert blocked.exit_code == 3
    assert "unknown_retry_confirmation_required" in blocked.stderr
    assert retried.exit_code == 0


def test_providers_and_report_retry_are_available() -> None:
    runtime = FakeRuntime()
    runner = CliRunner()

    providers = runner.invoke(
        create_app(lambda: runtime), ["providers", "--json", "--request-id", "req-2"]
    )
    retry = runner.invoke(
        create_app(lambda: runtime),
        ["report", "retry", "task-1", "--expected-version", "1"],
    )

    assert providers.exit_code == 0
    assert json.loads(providers.stdout)["data"][0]["provider_id"] == "fake"
    assert retry.exit_code == 0
