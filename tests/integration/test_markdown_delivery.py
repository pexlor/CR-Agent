from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from code_review_agent.adapters.output.markdown import MarkdownOutputAdapter
from code_review_agent.application.dto import BudgetReportView, TraceReportView
from code_review_agent.domain.execution.execution_models import (
    CandidateLocation,
    CandidateLocationKind,
)
from code_review_agent.domain.findings.models import Confidence
from code_review_agent.domain.report.builder import ReportBuilder
from tests.unit.domain.report.test_builder import _snapshot


def test_render_and_deliver_uses_task_scoped_atomic_target(tmp_path: Path):
    model = ReportBuilder().build(_snapshot())
    adapter = MarkdownOutputAdapter()
    target = tmp_path / "reports" / "task-1.md"

    rendered = adapter.render(model)
    result = adapter.deliver(model, target)

    assert rendered.endswith("\n")
    assert result.path == target
    assert target.read_text(encoding="utf-8") == rendered
    assert not list(target.parent.glob("*.tmp"))


def test_new_snapshot_replaces_same_task_target(tmp_path: Path):
    adapter = MarkdownOutputAdapter()
    target = tmp_path / "task-1.md"
    first = ReportBuilder().build(_snapshot())
    second_snapshot = _snapshot("partial")
    second = ReportBuilder().build(second_snapshot)

    adapter.deliver(first, target)
    adapter.deliver(second, target)

    assert "partial" in target.read_text(encoding="utf-8")


def test_render_presents_location_budget_and_trace_as_stable_markdown():
    finding = SimpleNamespace(
        finding_id="finding-1",
        title="Example finding",
        location=CandidateLocation(
            kind=CandidateLocationKind.NEW_LINE,
            file_id="file_abc123",
            line=42,
        ),
        severity="high",
        confidence=Confidence.HIGH,
        problem="A problem.",
        trigger_condition="A trigger.",
        impact="An impact.",
        suggestion="A suggestion.",
        confidence_basis="Direct evidence.",
        trace_id="trace-1",
    )
    snapshot = replace(
        _snapshot("complete_with_findings", findings=(finding,)),
        budget_summary=BudgetReportView(
            task_id="task-1",
            authorized=1000,
            known_consumption=400,
            uncertain_consumption=50,
            active_reservations=100,
            remaining_budget=450,
            overage=0,
            authorization_deficit=25,
            account_state="active",
            ledger_version=7,
        ),
        trace_summary=TraceReportView(
            task_id="task-1",
            event_count=12,
            model_call_count=3,
            accepted_count=2,
            rejected_count=1,
            error_count=1,
            query_command="uv run code-review-agent trace show task-1",
        ),
    )

    rendered = MarkdownOutputAdapter().render(ReportBuilder().build(snapshot))

    location_line = next(
        line for line in rendered.splitlines() if line.startswith("- Location:")
    )
    assert location_line == "- Location: `new_line file_abc123:42`"
    assert re.fullmatch(r"- Location: `[^`]+`", location_line)
    assert "| Metric | Tokens / Value |" in rendered
    for metric, value in (
        ("Authorized", "1000 token"),
        ("Known consumption", "400 token"),
        ("Uncertain consumption", "50 token"),
        ("Active reservations", "100 token"),
        ("Remaining", "450 token"),
        ("Overage", "0 token"),
        ("Authorization deficit", "25 token"),
        ("Account state", "active"),
        ("Ledger version", "7"),
    ):
        assert f"| {metric} | {value} |" in rendered
    for metric, value in (
        ("Event count", "12"),
        ("Model calls", "3"),
        ("Accepted", "2"),
        ("Rejected", "1"),
        ("Errors", "1"),
    ):
        assert f"| {metric} | {value} |" in rendered
    assert "`uv run code-review-agent trace show task-1`" in rendered
    assert "BudgetSummary(" not in rendered
    assert "namespace(" not in rendered
