from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from code_review_agent.application.dto import StartReviewCommand
from code_review_agent.application.persistence import ReviewStateStore
from code_review_agent.bootstrap import ConfiguredRuntime
from code_review_agent.config import CliConfig


def _config(state_database: Path) -> CliConfig:
    return CliConfig(
        state_database=state_database,
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


def _command(task_id: str, diff_text: str, tmp_path: Path) -> StartReviewCommand:
    return StartReviewCommand(
        task_id=task_id,
        diff_text=diff_text,
        diff_file=None,
        output_path=tmp_path / "reports" / f"{task_id}.md",
    )


EVAL_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,4 +1,4 @@\n"
    " def parse(expr):\n"
    "-    return expr\n"
    "+    return eval(expr)\n"
    " def main():\n"
    "     pass\n"
)


@respx.mock
def test_runtime_runs_declared_static_tool_and_traces_it(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"findings": []})}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    respx.post("https://api.example.com/v1/chat/completions").mock(
        side_effect=handler
    )
    config = _config(tmp_path / "state-tool.sqlite3")
    command = _command("task-tool-1", EVAL_DIFF, tmp_path)

    result = ConfiguredRuntime(config).review(
        command, "openai-compatible", "review-model", 50_000, "request-tool-1"
    )

    assert result.result_state == "complete_no_findings"
    # The declared builtin tool must actually run and its evidence must reach
    # the model prompt (AC-10: tool result traced, not a no-op).
    assert "dangerous-python-execution" in captured["body"]
    assert "python-eval-call" in captured["body"]

    events = ReviewStateStore(config.state_database).trace("task-tool-1")
    tool_events = [event for event in events if event.event_type.startswith("tool.")]
    assert any(event.event_type == "tool.succeeded" for event in tool_events)
    assert any(
        event.summary.get("tool_id") == "dangerous-python-execution"
        for event in tool_events
    )
