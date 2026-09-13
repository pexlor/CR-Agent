from __future__ import annotations

from types import SimpleNamespace

from code_review_agent.domain.common.digests import sha256_bytes
from code_review_agent.domain.execution.execution_models import (
    CandidateFinding,
    CandidateLocation,
    CandidateLocationKind,
    EvidenceReference,
)
from code_review_agent.domain.findings.models import (
    Confidence,
    FindingProcessingRequest,
)
from code_review_agent.domain.findings.processor import FindingProcessor


def _candidate(*, candidate_id: str = "candidate-1", location=None) -> CandidateFinding:
    return CandidateFinding(
        candidate_id=candidate_id,
        task_id="task-1",
        work_unit_id="unit-1",
        execution_id="execution-1",
        category="correctness",
        title="Unchecked result",
        problem="The result is ignored.",
        trigger_condition="When the call fails.",
        location=location
        or CandidateLocation(
            kind=CandidateLocationKind.NEW_LINE,
            file_id="file-1",
            line=2,
        ),
        evidence_refs=("evidence-1",),
        impact="The operation can silently fail.",
        suggestion="Handle the returned error.",
        change_causation="The added line drops the returned result.",
        limitations="",
        schema_version="1.0",
    )


def _request(candidate: CandidateFinding, *, semantic=True, tool=True, executions=None):
    evidence = EvidenceReference(
        evidence_id="evidence-1",
        evidence_type="changed_code",
        source_ref="line-2",
        location=candidate.location,
        content_digest=sha256_bytes(b"changed"),
    )
    execution = SimpleNamespace(
        execution_id="execution-1",
        task_id="task-1",
        work_unit_id="unit-1",
        plan_id="plan-1",
        state="succeeded",
        coverage_impact="fully_covered",
        candidates=(candidate,),
        evidence=(evidence,),
        tool_attempts=(
            SimpleNamespace(
                state="succeeded",
                result=SimpleNamespace(
                    success=True,
                    covered_file_ids=("file-1",),
                    rule_ids=("ERR001",),
                ),
            ),
        )
        if tool
        else (),
    )
    change_set = SimpleNamespace(
        task_id="task-1",
        change_set_id="changes-1",
        change_set_digest="c" * 64,
        files=(
            SimpleNamespace(
                file_id="file-1",
                old_path="src/a.py",
                new_path="src/a.py",
                change_type="modified",
                hunks=(
                    SimpleNamespace(
                        lines=(
                            SimpleNamespace(
                                line_type="addition",
                                new_line_number=2,
                                old_line_number=None,
                            ),
                            SimpleNamespace(
                                line_type="deletion",
                                new_line_number=None,
                                old_line_number=1,
                            ),
                        )
                    ),
                ),
            ),
        ),
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        task_id="task-1",
        input_binding_id="binding-1",
        change_set_id="changes-1",
        change_set_digest="c" * 64,
        coverage_scopes=(
            SimpleNamespace(
                scope_id="scope-1",
                disposition="planned",
                reason_code=None,
                file_id="file-1",
            ),
        ),
        work_units=(SimpleNamespace(work_unit_id="unit-1", scope_ids=("scope-1",)),),
    )
    return FindingProcessingRequest(
        request_id="request-1",
        task_id="task-1",
        execution_fact_boundary_id="checkpoint-1",
        change_set=change_set,
        plan=plan,
        executions=tuple((execution,) if executions is None else executions),
        language_semantics=("python.unhandled-result.v1",) if semantic else (),
    )


def test_processes_located_evidence_with_semantic_support_as_high_confidence():
    result = FindingProcessor().process(_request(_candidate()))

    assert len(result.finding_set.findings) == 1
    assert result.finding_set.findings[0].confidence is Confidence.HIGH
    assert result.finding_set.findings[0].location.line == 2


def test_model_self_score_is_not_an_input_to_confidence():
    candidate = _candidate()
    result = FindingProcessor().process(_request(candidate))

    assert result.finding_set.findings[0].confidence is Confidence.HIGH


def test_rejects_location_outside_the_change_set():
    candidate = _candidate(
        location=CandidateLocation(
            kind=CandidateLocationKind.NEW_LINE,
            file_id="file-1",
            line=99,
        )
    )

    result = FindingProcessor().process(_request(candidate))

    assert result.finding_set.findings == ()
    assert result.rejections[0].reason == "location_not_found"


def test_accepts_file_and_cross_file_locations_without_inventing_line_numbers():
    file_candidate = _candidate(
        location=CandidateLocation(kind=CandidateLocationKind.FILE, file_id="file-1")
    )
    cross_file_candidate = _candidate(
        candidate_id="candidate-2",
        location=CandidateLocation(
            kind=CandidateLocationKind.MULTI_FILE,
            file_id="file-1",
            related_file_ids=("file-1",),
        ),
    )

    file_result = FindingProcessor().process(_request(file_candidate))
    cross_file_result = FindingProcessor().process(_request(cross_file_candidate))

    assert file_result.finding_set.findings[0].location.line is None
    assert cross_file_result.finding_set.findings[0].location.line is None


def test_accepts_deleted_old_side_location():
    candidate = _candidate(
        location=CandidateLocation(
            kind=CandidateLocationKind.OLD_LINE,
            file_id="file-1",
            line=1,
        )
    )

    result = FindingProcessor().process(_request(candidate))

    assert result.finding_set.findings[0].location.line == 1


def test_tool_failure_keeps_actionable_finding_as_advisory():
    result = FindingProcessor().process(
        _request(_candidate(), semantic=False, tool=False)
    )

    assert result.finding_set.findings[0].confidence is Confidence.ADVISORY


def test_rejects_pre_existing_unrelated_and_non_actionable_candidates():
    for marker, reason in (
        ("pre_existing issue", "pre_existing_issue"),
        ("unrelated change", "outside_change_scope"),
        ("not_actionable", "not_actionable"),
    ):
        candidate = _candidate()
        object.__setattr__(candidate, "change_causation", marker)
        result = FindingProcessor().process(_request(candidate))

        assert result.finding_set.findings == ()
        assert result.rejections[0].reason == reason


def test_coverage_projects_missing_execution_to_unreviewed():
    request = _request(_candidate(), executions=())

    result = FindingProcessor().process(request)

    assert result.finding_set.coverage.entries[0].state.value == "unreviewed"


def test_coverage_preserves_degraded_execution_error_code():
    request = _request(_candidate())
    execution = request.executions[0]
    execution.coverage_impact = "degraded"
    execution.error_code = "model_output_invalid"

    result = FindingProcessor().process(request)

    entry = result.finding_set.coverage.entries[0]
    assert entry.state.value == "unreviewed"
    assert entry.reason_code == "model_output_invalid"
