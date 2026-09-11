"""Build presentation data from one immutable result snapshot."""

from __future__ import annotations

from code_review_agent.domain.report.models import (
    CompletionView,
    ReportModel,
    ResultSnapshot,
    ResultSnapshotKind,
)


class ReportBuilder:
    def build(self, snapshot: ResultSnapshot) -> ReportModel:
        if snapshot.result_state == "pending":
            raise ValueError("snapshot_not_reportable")
        if snapshot.kind is ResultSnapshotKind.FAILURE:
            completion = CompletionView(
                "failed",
                "审查未形成可报告结果。",
                (snapshot.failure_reason or "审查在固定阶段失败。",),
                "修复失败原因后重新发起审查。",
            )
            return ReportModel(
                "failure",
                snapshot.task_id,
                snapshot.snapshot_id,
                snapshot.snapshot_version,
                snapshot.checkpoint_id,
                snapshot.subject,
                completion,
                (),
                (),
                snapshot.budget_summary,
                snapshot.unknown_attempts,
                snapshot.trace_summary,
                snapshot.security_summary,
            )

        finding_set = snapshot.finding_set
        findings = tuple(finding_set.findings)
        state = snapshot.result_state
        conclusion = {
            "no_changes": "没有需要审查的文本变更。",
            "complete_no_findings": "未发现有效问题。",
            "complete_with_findings": f"发现 {len(findings)} 条可供判断的问题。",
            "partial": "审查部分完成，结果不能代表未审查范围。",
            "unknown": "审查结果存在未确定部分。",
        }.get(state)
        if conclusion is None:
            raise ValueError("report_model_invalid")
        return ReportModel(
            "review",
            snapshot.task_id,
            snapshot.snapshot_id,
            snapshot.snapshot_version,
            snapshot.checkpoint_id,
            snapshot.subject,
            CompletionView(
                state,
                conclusion,
                (f"failure stage: {snapshot.failure_stage}",)
                if snapshot.failure_stage
                else (),
                "核对未审查范围并决定是否恢复审查。",
            ),
            tuple(finding_set.coverage.entries),
            findings,
            snapshot.budget_summary,
            snapshot.unknown_attempts,
            snapshot.trace_summary,
            snapshot.security_summary,
        )
