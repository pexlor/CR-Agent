from __future__ import annotations

from pathlib import Path

from code_review_agent.adapters.output.markdown import MarkdownOutputAdapter
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
