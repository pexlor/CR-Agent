from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from code_review_agent.adapters.input.plain_diff import PlainDiffProvider
from code_review_agent.domain.input.models import (
    ChangeType,
    CompletenessStatus,
    CoverageStatus,
    LineType,
)
from code_review_agent.domain.input.service import InputService

from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.common.time import FixedClock
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ScanResult,
    SecurityDecision,
    SecurityFinding,
    SensitiveCategory,
)
from code_review_agent.domain.security.policy import SecurityPolicy
from code_review_agent.domain.security.service import SecurityService

FIXTURES = Path("tests/fixtures/diffs")


class Scanner:
    def __init__(self, marker: str | None = None) -> None:
        self.marker = marker

    def scan(
        self,
        content: str,
        descriptor: ArtifactDescriptor,
        policy: SecurityPolicy,
    ) -> ScanResult:
        if self.marker is None or self.marker not in content:
            return ScanResult.complete((), policy.detector_manifest)
        start = content.index(self.marker)
        return ScanResult.complete(
            (
                SecurityFinding(
                    detector_id="fixture-scanner",
                    category=SensitiveCategory.CREDENTIAL,
                    start=start,
                    end=start + len(self.marker),
                    confidence="high",
                    rule_id="simulated-marker",
                    crosses_boundary=False,
                    action="redact",
                    summary="simulated credential marker",
                ),
            ),
            policy.detector_manifest,
        )


def make_service(marker: str | None = None) -> InputService:
    policy = SecurityPolicy(
        policy_id="input-test",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset({SensitiveCategory.CREDENTIAL}),
        detector_manifest=("fixture-scanner@1",),
        action_matrix={
            SensitiveCategory.CREDENTIAL: SecurityDecision.REDACTED,
        },
        purpose_transitions=frozenset(),
    )
    security = SecurityService(Scanner(marker), policy=policy)
    return InputService(
        PlainDiffProvider(security),
        security,
        clock=FixedClock(datetime(2026, 9, 10, 8, 0, tzinfo=UTC)),
    )


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_empty_diff_is_complete_no_change_input() -> None:
    result = make_service().normalize_plain_diff(task_id="task-1", text="")

    assert result.change_set.files == ()
    assert result.change_set.file_count == 0
    assert result.change_set.changed_line_count == 0
    assert result.change_set.completeness.status is CompletenessStatus.COMPLETE
    assert result.change_set.coverage.status is CoverageStatus.COMPLETE
    assert result.binding.content_digest == result.change_set.identity.content_digest


def test_pure_deletion_preserves_old_side_line_location() -> None:
    result = make_service().normalize_plain_diff(
        task_id="task-1", text=fixture("pure_delete.diff")
    )

    changed_file = result.change_set.files[0]
    assert changed_file.change_type is ChangeType.DELETED
    assert changed_file.old_path == "removed.py"
    assert changed_file.new_path is None
    deleted = changed_file.hunks[0].lines[0]
    assert deleted.line_type is LineType.DELETION
    assert deleted.old_line_number == 1
    assert deleted.new_line_number is None


def test_rename_preserves_path_relationship() -> None:
    result = make_service().normalize_plain_diff(
        task_id="task-1", text=fixture("rename.diff")
    )

    changed_file = result.change_set.files[0]
    assert changed_file.change_type is ChangeType.RENAMED
    assert changed_file.old_path == "old_name.py"
    assert changed_file.new_path == "new_name.py"
    assert changed_file.hunks == ()


def test_binary_file_is_complete_input_but_unreviewable_scope() -> None:
    result = make_service().normalize_plain_diff(
        task_id="task-1", text=fixture("binary.diff")
    )

    changed_file = result.change_set.files[0]
    assert changed_file.change_type is ChangeType.BINARY
    assert changed_file.is_binary is True
    assert changed_file.unreviewable_reason == "binary_content"
    assert result.change_set.completeness.status is CompletenessStatus.COMPLETE
    assert result.change_set.coverage.status is CoverageStatus.PARTIAL
    assert result.change_set.coverage.unreviewable_file_ids == (changed_file.file_id,)


def test_text_and_file_forms_build_the_same_stable_review_object() -> None:
    text = fixture("basic.diff")
    text_result = make_service().normalize_plain_diff(task_id="task-1", text=text)
    file_result = make_service().normalize_plain_diff(
        task_id="task-1", file_path=FIXTURES / "basic.diff"
    )

    assert text_result.change_set.identity == file_result.change_set.identity
    assert text_result.change_set.change_set_id == file_result.change_set.change_set_id
    assert [item.file_id for item in text_result.change_set.files] == [
        item.file_id for item in file_result.change_set.files
    ]


@pytest.mark.parametrize(
    "content",
    [
        "this is not a diff\nSIMULATED_PRIVATE_VALUE",
        (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,1 @@\n"
            "-only-one-old-line\n"
        ),
    ],
)
def test_malformed_input_returns_safe_stable_error(content: str) -> None:
    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert "SIMULATED_PRIVATE_VALUE" not in repr(captured.value)
    assert "SIMULATED_PRIVATE_VALUE" not in str(captured.value.to_dict())


@pytest.mark.parametrize(
    "old_path,new_path",
    [
        ("a/../private.py", "b/../private.py"),
        ("/absolute.py", "/absolute.py"),
        ("a/C:\\private.py", "b/C:\\private.py"),
    ],
)
def test_dangerous_diff_paths_are_rejected_without_echoing_them(
    old_path: str, new_path: str
) -> None:
    content = (
        f"diff --git {old_path} {new_path}\n"
        f"--- {old_path}\n"
        f"+++ {new_path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert old_path not in str(captured.value.to_dict())


def test_more_than_200_files_is_rejected_as_a_whole() -> None:
    content = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n"
        f"--- a/f{i}.py\n"
        f"+++ b/f{i}.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        for i in range(201)
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_too_large"
    assert captured.value.details == {"limit": 200, "metric": "files"}


def test_more_than_10000_changed_lines_is_rejected_as_a_whole() -> None:
    deleted = "".join(f"-old-{i}\n" for i in range(5_001))
    added = "".join(f"+new-{i}\n" for i in range(5_000))
    content = (
        "diff --git a/large.py b/large.py\n"
        "--- a/large.py\n"
        "+++ b/large.py\n"
        "@@ -1,5001 +1,5000 @@\n"
        f"{deleted}{added}"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_too_large"
    assert captured.value.details == {"limit": 10_000, "metric": "changed_lines"}


def test_sanitized_content_is_referenced_without_raw_diff_in_result() -> None:
    marker = "SIMULATED_TOKEN_VALUE"
    content = (
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1 +1 @@\n"
        "-value = 'old'\n"
        f"+value = '{marker}'\n"
    )

    result = make_service(marker).normalize_plain_diff(task_id="task-1", text=content)

    assert marker not in repr(result)
    added = result.change_set.files[0].hunks[0].lines[1]
    assert added.content_ref.artifact.decision is SecurityDecision.REDACTED
    assert added.content_ref.content_digest


def test_missing_or_multiple_plain_diff_sources_are_distinct_errors() -> None:
    with pytest.raises(StableError) as missing:
        make_service().normalize_plain_diff(task_id="task-1")
    with pytest.raises(StableError) as multiple:
        make_service().normalize_plain_diff(
            task_id="task-1",
            text=fixture("basic.diff"),
            file_path=FIXTURES / "basic.diff",
        )

    assert missing.value.code == "input_missing"
    assert multiple.value.code == "input_multiple_sources"
