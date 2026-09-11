from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from code_review_agent.domain.findings.models import (
    CoverageEntry,
    CoverageSnapshot,
    CoverageState,
    FindingSet,
)
from code_review_agent.domain.report.builder import ReportBuilder
from code_review_agent.domain.report.models import (
    ReportModel,
    ResultSnapshot,
    ResultSnapshotKind,
)


def _finding_set() -> FindingSet:
    coverage = CoverageSnapshot(
        task_id="task-1",
        plan_id="plan-1",
        execution_fact_boundary_id="checkpoint-1",
        entries=(CoverageEntry("scope-1", CoverageState.REVIEWED, "success"),),
    )
    return FindingSet(
        task_id="task-1",
        plan_id="plan-1",
        execution_fact_boundary_id="checkpoint-1",
        findings=(),
        coverage=coverage,
        rejections=(),
    )


def _snapshot(state: str = "complete_no_findings", findings=None) -> ResultSnapshot:
    finding_set = _finding_set()
    if findings is not None:
        finding_set = SimpleNamespace(
            task_id="task-1",
            plan_id="plan-1",
            execution_fact_boundary_id="checkpoint-1",
            findings=tuple(findings),
            coverage=finding_set.coverage,
            content_digest="f" * 64,
        )
    return ResultSnapshot(
        snapshot_id="snapshot-1",
        task_id="task-1",
        snapshot_version=1,
        kind=ResultSnapshotKind.REVIEW,
        result_state=state,
        input_binding=SimpleNamespace(
            binding_id="binding-1", task_id="task-1", content_digest="a" * 64
        ),
        review_plan=SimpleNamespace(plan_id="plan-1", task_id="task-1"),
        finding_set=finding_set,
        budget_summary=SimpleNamespace(task_id="task-1", ledger_version=1),
        unknown_attempts=(),
        trace_summary=SimpleNamespace(task_id="task-1"),
        checkpoint_id="checkpoint-1",
        subject="plain diff",
        security_summary="policy-1/v1",
    )


def test_builds_complete_no_findings_without_claiming_no_defects():
    model = ReportBuilder().build(_snapshot())

    assert isinstance(model, ReportModel)
    assert model.completion.state == "complete_no_findings"
    assert "未发现有效问题" in model.completion.conclusion
    assert "不存在缺陷" not in model.completion.conclusion


def test_builds_failure_without_fabricating_review_references():
    snapshot = ResultSnapshot(
        snapshot_id="snapshot-2",
        task_id="task-1",
        snapshot_version=1,
        kind=ResultSnapshotKind.FAILURE,
        result_state="failed",
        input_binding=None,
        review_plan=None,
        finding_set=None,
        budget_summary=SimpleNamespace(task_id="task-1", ledger_version=1),
        unknown_attempts=(),
        trace_summary=SimpleNamespace(task_id="task-1"),
        checkpoint_id="checkpoint-1",
        subject="plain diff",
        security_summary="policy-1/v1",
        failure_stage="input",
        failure_reason="input_invalid",
    )

    model = ReportBuilder().build(snapshot)

    assert model.kind == "failure"
    assert model.coverage == ()
    assert model.findings == ()


def test_rejects_cross_task_snapshot_references():
    with pytest.raises(ValueError, match="task"):
        replace(
            _snapshot(),
            input_binding=SimpleNamespace(
                binding_id="binding-1",
                task_id="other-task",
                content_digest="a" * 64,
            ),
        )
