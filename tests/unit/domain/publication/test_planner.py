from __future__ import annotations

from types import SimpleNamespace

from code_review_agent.domain.execution.execution_models import (
    CandidateLocationKind,
)
from code_review_agent.domain.publication.models import PublicationItemKind
from code_review_agent.domain.publication.planner import PublicationPlanner


def _finding(
    finding_id: str,
    fingerprint: str,
    kind: CandidateLocationKind,
    file_id: str,
    line: int | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        finding_id=finding_id,
        fingerprint=fingerprint,
        title=f"title-{finding_id}",
        confidence=SimpleNamespace(value="high"),
        problem="problem",
        trigger_condition="trigger",
        impact="impact",
        suggestion="suggestion",
        trace_id=f"trace-{finding_id}",
        limitations="none",
        location=SimpleNamespace(kind=kind, file_id=file_id, line=line),
    )


def _review() -> tuple[str, SimpleNamespace, SimpleNamespace]:
    files = (
        SimpleNamespace(
            file_id="file-a",
            old_path="src/a.py",
            new_path="src/a.py",
            hunks=(
                SimpleNamespace(
                    lines=(
                        SimpleNamespace(
                            line_type=SimpleNamespace(value="addition"),
                            old_line_number=None,
                            new_line_number=12,
                        ),
                    )
                ),
            ),
        ),
        SimpleNamespace(
            file_id="file-b",
            old_path="src/b.py",
            new_path="src/b.py",
            hunks=(
                SimpleNamespace(
                    lines=(
                        SimpleNamespace(
                            line_type=SimpleNamespace(value="deletion"),
                            old_line_number=8,
                            new_line_number=None,
                        ),
                    )
                ),
            ),
        ),
    )
    identity = SimpleNamespace(
        provider_id="github",
        repository_identity="owner/repo",
        object_number=7,
        base_sha="a" * 40,
        start_sha="b" * 40,
        head_sha="c" * 40,
    )
    normalized = SimpleNamespace(
        change_set=SimpleNamespace(identity=identity, files=files)
    )
    findings = (
        _finding("new", "1" * 64, CandidateLocationKind.NEW_LINE, "file-a", 12),
        _finding("old", "2" * 64, CandidateLocationKind.OLD_LINE, "file-b", 8),
        _finding("file", "3" * 64, CandidateLocationKind.FILE, "file-b", None),
    )
    snapshot = SimpleNamespace(
        task_id="task-1",
        snapshot_id="snapshot-1",
        snapshot_version=1,
        result_state="complete_with_findings",
        finding_set=SimpleNamespace(
            findings=findings, coverage=SimpleNamespace(entries=())
        ),
        budget_summary=SimpleNamespace(
            authorized=100,
            known_consumption=25,
            remaining_budget=75,
            currency="CNY",
        ),
        trace_summary=SimpleNamespace(event_count=4),
        failure_stage=None,
    )
    return "https://github.com/owner/repo/pull/7", normalized, snapshot


def test_planner_maps_changed_lines_and_keeps_file_finding_in_summary() -> None:
    source_url, normalized, snapshot = _review()

    plan = PublicationPlanner().plan(source_url, normalized, snapshot)

    assert [item.kind for item in plan.items] == [
        PublicationItemKind.LINE,
        PublicationItemKind.LINE,
        PublicationItemKind.SUMMARY,
    ]
    assert plan.items[0].position is not None
    assert plan.items[0].position.side == "RIGHT"
    assert plan.items[0].position.path == "src/a.py"
    assert plan.items[1].position is not None
    assert plan.items[1].position.side == "LEFT"
    assert plan.items[1].position.path == "src/b.py"
    assert "title-file" in plan.items[-1].body
    assert "<!-- cr-agent:publication:" in plan.items[-1].body


def test_publication_keys_are_stable_and_snapshot_bound() -> None:
    source_url, normalized, snapshot = _review()

    first = PublicationPlanner().plan(source_url, normalized, snapshot)
    second = PublicationPlanner().plan(source_url, normalized, snapshot)
    changed = PublicationPlanner().plan(
        source_url,
        normalized,
        SimpleNamespace(**{**vars(snapshot), "snapshot_id": "snapshot-2"}),
    )

    assert first == second
    assert first.publication_id != changed.publication_id
    assert {item.publication_key for item in first.items}.isdisjoint(
        item.publication_key for item in changed.items
    )


def test_planner_rejects_source_identity_mismatch() -> None:
    source_url, normalized, snapshot = _review()

    try:
        PublicationPlanner().plan(
            source_url.replace("owner/repo", "other/repo"), normalized, snapshot
        )
    except ValueError as exc:
        assert str(exc) == "publication_target_mismatch"
    else:
        raise AssertionError("mismatched source URL was accepted")


def test_planner_accepts_gitlab_urls_with_and_without_dash_separator() -> None:
    source_url, normalized, snapshot = _review()
    normalized.change_set.identity.provider_id = "gitlab"
    normalized.change_set.identity.repository_identity = "group/repo"

    dashed = PublicationPlanner().plan(
        "https://gitlab.com/group/repo/-/merge_requests/7", normalized, snapshot
    )
    plain = PublicationPlanner().plan(
        "https://gitlab.com/group/repo/merge_requests/7", normalized, snapshot
    )

    assert dashed.target.repository == "group/repo"
    assert plain.target.repository == "group/repo"


def test_planner_rejects_a_line_not_present_in_the_fixed_diff() -> None:
    source_url, normalized, snapshot = _review()
    snapshot.finding_set.findings[0].location.line = 999

    try:
        PublicationPlanner().plan(source_url, normalized, snapshot)
    except ValueError as exc:
        assert str(exc) == "publication_position_invalid"
    else:
        raise AssertionError("line outside the fixed diff was accepted")
