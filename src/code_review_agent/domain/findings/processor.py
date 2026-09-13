"""Pure, deterministic validation and consolidation of candidate findings."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.execution.execution_models import (
    CandidateLocationKind,
)
from code_review_agent.domain.findings.models import (
    Confidence,
    CoverageEntry,
    CoverageSnapshot,
    CoverageState,
    FinalFinding,
    FindingProcessingRequest,
    FindingProcessingResult,
    FindingRejection,
    FindingSet,
)


class FindingProcessor:
    """Convert execution-owned candidates into reportable final findings."""

    def process(self, request: FindingProcessingRequest) -> FindingProcessingResult:
        files = {item.file_id: item for item in request.change_set.files}
        executions = {item.execution_id: item for item in request.executions}
        work_units = {item.work_unit_id: item for item in request.plan.work_units}
        evidence_by_execution = {
            item.execution_id: {
                evidence.evidence_id: evidence for evidence in item.evidence
            }
            for item in request.executions
        }
        rejections: list[FindingRejection] = []
        accepted: dict[str, list[tuple[Any, Any, dict[str, Any]]]] = defaultdict(list)

        for execution in request.executions:
            for candidate in execution.candidates:
                rejection = self._validate_candidate(
                    candidate,
                    execution,
                    files,
                    work_units,
                    evidence_by_execution[execution.execution_id],
                )
                if rejection is not None:
                    rejections.append(rejection)
                    continue
                fingerprint = self._fingerprint(request, candidate)
                accepted[fingerprint].append(
                    (
                        candidate,
                        execution,
                        evidence_by_execution[execution.execution_id],
                    )
                )

        findings = tuple(
            sorted(
                (
                    self._finalize(request, fingerprint, contributions)
                    for fingerprint, contributions in accepted.items()
                ),
                key=lambda item: (
                    self._severity_rank(item.severity),
                    0 if item.confidence is Confidence.HIGH else 1,
                    item.location.file_id,
                    item.location.kind.value,
                    item.location.line or 0,
                    item.category,
                    item.fingerprint,
                ),
            )
        )
        coverage = self._coverage(request, executions)
        finding_set = FindingSet(
            task_id=request.task_id,
            plan_id=request.plan.plan_id,
            execution_fact_boundary_id=request.execution_fact_boundary_id,
            findings=findings,
            coverage=coverage,
            rejections=tuple(rejections),
        )
        return FindingProcessingResult(finding_set, tuple(rejections))

    def _validate_candidate(
        self,
        candidate: Any,
        execution: Any,
        files: dict[str, Any],
        work_units: dict[str, Any],
        evidence: dict[str, Any],
    ) -> FindingRejection | None:
        if (
            candidate.task_id != execution.task_id
            or candidate.execution_id != execution.execution_id
        ):
            return FindingRejection(candidate.candidate_id, "invalid_structure")
        if candidate.work_unit_id not in work_units:
            return FindingRejection(candidate.candidate_id, "invalid_structure")
        if not self._location_exists(candidate.location, files):
            return FindingRejection(candidate.candidate_id, "location_not_found")
        refs = [evidence.get(ref) for ref in candidate.evidence_refs]
        if any(item is None for item in refs):
            return FindingRejection(candidate.candidate_id, "evidence_not_found")
        evidence_items: list[Any] = [evidence[ref] for ref in candidate.evidence_refs]
        if not any(
            item.evidence_type in {"changed_code", "change_relation"}
            for item in evidence_items
        ):
            return FindingRejection(candidate.candidate_id, "evidence_mismatch")
        causation = candidate.change_causation.lower()
        if "pre_existing" in causation or "pre-existing" in causation:
            return FindingRejection(candidate.candidate_id, "pre_existing_issue")
        if "unrelated" in causation:
            return FindingRejection(candidate.candidate_id, "outside_change_scope")
        if "not_actionable" in causation or not candidate.suggestion.strip():
            return FindingRejection(candidate.candidate_id, "not_actionable")
        return None

    @staticmethod
    def _location_exists(location: Any, files: dict[str, Any]) -> bool:
        changed_file = files.get(location.file_id)
        if changed_file is None:
            return False
        if location.kind is CandidateLocationKind.FILE:
            return True
        if location.kind is CandidateLocationKind.MULTI_FILE:
            return all(file_id in files for file_id in location.related_file_ids)
        line_type = (
            "addition"
            if location.kind is CandidateLocationKind.NEW_LINE
            else "deletion"
        )
        for hunk in changed_file.hunks:
            for line in hunk.lines:
                actual_line_type = getattr(line.line_type, "value", line.line_type)
                if (
                    actual_line_type == line_type
                    and (
                        line.new_line_number
                        if line_type == "addition"
                        else line.old_line_number
                    )
                    == location.line
                ):
                    return True
        return False

    @staticmethod
    def _fingerprint(request: FindingProcessingRequest, candidate: Any) -> str:
        return sha256_digest(
            {
                "change_set": request.change_set.change_set_digest,
                "category": candidate.category.strip().lower(),
                "mechanism": candidate.change_causation.strip().lower(),
                "location": [
                    candidate.location.kind.value,
                    candidate.location.file_id,
                    list(candidate.location.related_file_ids),
                ],
            }
        )

    def _finalize(
        self,
        request: FindingProcessingRequest,
        fingerprint: str,
        contributions: list[tuple[Any, Any, dict[str, Any]]],
    ) -> FinalFinding:
        candidate, execution, evidence = contributions[0]
        direct = tuple(
            ref
            for ref in candidate.evidence_refs
            if evidence[ref].evidence_type in {"changed_code", "change_relation"}
        )
        tool_verified = any(
            getattr(attempt, "state", None) == "succeeded"
            and getattr(getattr(attempt, "result", None), "success", False)
            for attempt in execution.tool_attempts
        )
        semantic_verified = bool(request.language_semantics)
        confidence = (
            Confidence.HIGH
            if direct and (tool_verified or semantic_verified)
            else Confidence.ADVISORY
        )
        finding_id = sha256_digest(
            {"task_id": request.task_id, "fingerprint": fingerprint}
        )
        return FinalFinding(
            finding_id=finding_id,
            fingerprint=fingerprint,
            task_id=request.task_id,
            category=candidate.category,
            severity="medium",
            confidence=confidence,
            title=candidate.title,
            problem=candidate.problem,
            trigger_condition=candidate.trigger_condition,
            impact=candidate.impact,
            suggestion=candidate.suggestion,
            location=candidate.location,
            evidence_ids=direct,
            trace_id=sha256_digest(
                {
                    "finding_id": finding_id,
                    "boundary": request.execution_fact_boundary_id,
                }
            ),
            source_candidate_ids=tuple(item[0].candidate_id for item in contributions),
            confidence_basis=(
                "direct changed-code evidence plus "
                + (
                    "deterministic tool verification"
                    if tool_verified
                    else "language semantics"
                )
            ),
            limitations=candidate.limitations,
        )

    @staticmethod
    def _severity_rank(severity: str) -> int:
        return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(severity, 4)

    def _coverage(
        self, request: FindingProcessingRequest, executions: dict[str, Any]
    ) -> CoverageSnapshot:
        entries = []
        for scope in request.plan.coverage_scopes:
            disposition = getattr(scope.disposition, "value", scope.disposition)
            if disposition == "unreviewable":
                state, reason = (
                    CoverageState.UNREVIEWABLE,
                    scope.reason_code or "unreviewable",
                )
            elif disposition == "no_review_required":
                state, reason = CoverageState.REVIEWED, "no_text_review_object"
            else:
                related = [
                    item
                    for item in executions.values()
                    if scope.scope_id
                    in next(
                        (
                            unit.scope_ids
                            for unit in request.plan.work_units
                            if unit.work_unit_id == item.work_unit_id
                        ),
                        (),
                    )
                ]
                execution = related[-1] if related else None
                execution_state = (
                    getattr(execution.state, "value", execution.state)
                    if execution is not None
                    else None
                )
                if execution is None or execution_state in {"failed", "partial"}:
                    state, reason = (
                        CoverageState.UNREVIEWED,
                        getattr(execution, "error_code", None) or "not_completed",
                    )
                elif execution_state == "unknown":
                    state, reason = CoverageState.UNKNOWN, "execution_unknown"
                elif (
                    getattr(
                        execution.coverage_impact, "value", execution.coverage_impact
                    )
                    == "fully_covered"
                ):
                    state, reason = CoverageState.REVIEWED, "execution_succeeded"
                else:
                    state, reason = CoverageState.UNREVIEWED, "coverage_degraded"
            entries.append(CoverageEntry(scope.scope_id, state, reason))
        return CoverageSnapshot(
            task_id=request.task_id,
            plan_id=request.plan.plan_id,
            execution_fact_boundary_id=request.execution_fact_boundary_id,
            entries=tuple(entries),
        )
