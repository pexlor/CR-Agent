from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import code_review_agent.domain.input.service as input_service_module
from code_review_agent.adapters.input.plain_diff import PlainDiffProvider
from code_review_agent.domain.common.digests import sha256_bytes
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.common.time import Clock, FixedClock
from code_review_agent.domain.input.models import (
    ChangeType,
    CompletenessStatus,
    CoverageStatus,
    InputLimits,
    LineType,
    NormalizedInput,
)
from code_review_agent.domain.input.service import InputService
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
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


def make_service(
    marker: str | None = None, *, clock: Clock | None = None
) -> InputService:
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
        clock=clock or FixedClock(datetime(2026, 9, 10, 8, 0, tzinfo=UTC)),
    )


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def diff_with_metadata(*metadata: str) -> str:
    lines = "".join(f"{line}\n" for line in metadata)
    return (
        "diff --git a/app.py b/app.py\n"
        f"{lines}"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def test_empty_diff_is_complete_no_change_input() -> None:
    result = make_service().normalize_plain_diff(task_id="task-1", text="")

    assert result.change_set.files == ()
    assert result.change_set.file_count == 0
    assert result.change_set.changed_line_count == 0
    assert result.change_set.completeness.status is CompletenessStatus.COMPLETE
    assert result.change_set.coverage.status is CoverageStatus.COMPLETE
    assert result.binding.content_digest == result.change_set.identity.content_digest


def test_empty_diff_is_resolved_through_security_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = SecurityPolicy(
        policy_id="empty-input",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset({SensitiveCategory.CREDENTIAL}),
        detector_manifest=("fixture-scanner@1",),
        action_matrix={SensitiveCategory.CREDENTIAL: SecurityDecision.REDACTED},
        purpose_transitions=frozenset(),
    )
    security = SecurityService(Scanner(), policy=policy)
    original_resolve = security.resolve
    resolved = 0

    def recording_resolve(*args: object, **kwargs: object) -> str:
        nonlocal resolved
        resolved += 1
        return original_resolve(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(security, "resolve", recording_resolve)
    service = InputService(
        PlainDiffProvider(security),
        security,
        clock=FixedClock(datetime(2026, 9, 10, 8, 0, tzinfo=UTC)),
    )

    result = service.normalize_plain_diff(task_id="task-1", text="")

    assert result.change_set.files == ()
    assert resolved == 1


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


def test_deleted_binary_uses_dev_null_without_changing_header_path() -> None:
    content = (
        "diff --git a/image.bin b/image.bin\n"
        "deleted file mode 100644\n"
        "Binary files a/image.bin and /dev/null differ\n"
    )

    result = make_service().normalize_plain_diff(task_id="task-1", text=content)

    changed_file = result.change_set.files[0]
    assert changed_file.old_path == "image.bin"
    assert changed_file.new_path is None


@pytest.mark.parametrize(
    "binary_line",
    [
        "Binary files /dev/null and b/image.bin differ",
        "Binary files a/image.bin and /dev/null differ",
    ],
)
def test_binary_dev_null_requires_matching_file_mode(binary_line: str) -> None:
    content = f"diff --git a/image.bin b/image.bin\n{binary_line}\n"

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("mode_line", "binary_line"),
    [
        ("new file mode 100644", "Binary files /dev/null and b/image.bin differ"),
        (
            "deleted file mode 100644",
            "Binary files a/image.bin and /dev/null differ",
        ),
    ],
)
def test_binary_paths_cannot_hide_conflicting_declared_markers(
    mode_line: str, binary_line: str
) -> None:
    content = (
        "diff --git a/image.bin b/image.bin\n"
        f"{mode_line}\n"
        "--- a/image.bin\n"
        "+++ b/image.bin\n"
        f"{binary_line}\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


def test_repeated_old_new_marker_pair_is_rejected() -> None:
    content = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "content",
    [
        diff_with_metadata("similarity index 101%"),
        diff_with_metadata("dissimilarity index unknown"),
        diff_with_metadata("old mode invalid", "new mode 100755"),
        diff_with_metadata("old mode 100644", "new mode 777777"),
        diff_with_metadata("index invalid"),
        diff_with_metadata("index 1234567..abcdef0 777777"),
    ],
)
def test_invalid_extended_metadata_is_rejected(content: str) -> None:
    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "metadata",
    [
        ("index 1234567..abcdef0 100644", "index 1234567..abcdef0 100644"),
        ("old mode 100644",),
        ("new mode 100755",),
        ("old mode 100644", "new mode 100644"),
        ("similarity index 90%", "similarity index 90%"),
        ("similarity index 90%", "dissimilarity index 10%"),
        ("similarity index 90%",),
    ],
)
def test_unpaired_duplicate_or_conflicting_metadata_is_rejected(
    metadata: tuple[str, ...],
) -> None:
    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(
            task_id="task-1", text=diff_with_metadata(*metadata)
        )

    assert captured.value.code == "input_malformed"


def test_file_lifecycle_mode_conflicts_with_old_new_mode_pair() -> None:
    content = (
        "diff --git a/app.py b/app.py\n"
        "new file mode 100644\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "--- /dev/null\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1 @@\n"
        "+new\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"


def test_duplicate_rename_metadata_is_rejected() -> None:
    content = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"


@pytest.mark.parametrize(
    ("mode_line", "binary_line"),
    [
        ("new file mode not-a-mode", "Binary files /dev/null and b/image.bin differ"),
        ("new file mode 10064", "Binary files /dev/null and b/image.bin differ"),
        (
            "deleted file mode 777777",
            "Binary files a/image.bin and /dev/null differ",
        ),
        (
            "deleted file mode 100644 trailing",
            "Binary files a/image.bin and /dev/null differ",
        ),
    ],
)
def test_binary_dev_null_rejects_invalid_git_file_mode(
    mode_line: str, binary_line: str
) -> None:
    content = f"diff --git a/image.bin b/image.bin\n{mode_line}\n{binary_line}\n"

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("mode_line", "binary_line"),
    [
        ("new file mode 100644", "Binary files /dev/null and b/image.bin differ"),
        (
            "deleted file mode 100644",
            "Binary files a/image.bin and /dev/null differ",
        ),
    ],
)
def test_duplicate_file_mode_declarations_are_rejected(
    mode_line: str, binary_line: str
) -> None:
    content = (
        f"diff --git a/image.bin b/image.bin\n{mode_line}\n{mode_line}\n{binary_line}\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "literal 0\nSIMULATED_BINARY_PAYLOAD\n",
    ],
)
def test_git_binary_patch_is_rejected_instead_of_marked_complete(payload: str) -> None:
    content = (
        "diff --git a/image.bin b/image.bin\n"
        "index 1234567..abcdef0 100644\n"
        "GIT binary patch\n"
        f"{payload}"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None
    assert "SIMULATED_BINARY_PAYLOAD" not in str(captured.value.to_dict())


@pytest.mark.parametrize(
    "binary_line",
    [
        "Binary files a/other.bin and b/image.bin differ",
        "Binary files a/image.bin and b/other.bin differ",
        "Binary files /dev/null and b/other.bin differ",
    ],
)
def test_binary_paths_cannot_replace_diff_header_identity(binary_line: str) -> None:
    content = (
        f"diff --git a/image.bin b/image.bin\nnew file mode 100644\n{binary_line}\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None
    assert "other.bin" not in str(captured.value.to_dict())


@pytest.mark.parametrize(
    "mode_lines",
    [
        "new file mode 100644\ndeleted file mode 100644\n",
        "new file mode 100644\n",
    ],
)
def test_conflicting_binary_file_modes_are_stable_malformed_errors(
    mode_lines: str,
) -> None:
    content = (
        "diff --git a/image.bin b/image.bin\n"
        f"{mode_lines}"
        "Binary files a/image.bin and b/image.bin differ\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


def test_duplicate_file_sections_are_rejected() -> None:
    content = fixture("basic.diff") + fixture("basic.diff")

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


def test_aggregation_value_error_is_mapped_without_exception_chain() -> None:
    class InvalidClock:
        def now(self) -> datetime:
            return datetime(2026, 9, 10, 8, 0)

    with pytest.raises(StableError) as captured:
        make_service(clock=InvalidClock()).normalize_plain_diff(
            task_id="task-1", text=fixture("basic.diff")
        )

    assert captured.value.code == "input_malformed"
    assert captured.value.__cause__ is None


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


def test_native_space_path_is_preserved() -> None:
    content = (
        "diff --git a/my file.py b/my file.py\n"
        "--- a/my file.py\n"
        "+++ b/my file.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    changed_file = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=content)
        .change_set.files[0]
    )

    assert changed_file.old_path == "my file.py"
    assert changed_file.new_path == "my file.py"


def test_git_c_style_octal_utf8_path_is_decoded() -> None:
    quoted_old = '"a/\\344\\270\\255\\346\\226\\207.py"'
    quoted_new = '"b/\\344\\270\\255\\346\\226\\207.py"'
    content = (
        f"diff --git {quoted_old} {quoted_new}\n"
        f"--- {quoted_old}\n"
        f"+++ {quoted_new}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    changed_file = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=content)
        .change_set.files[0]
    )

    assert changed_file.old_path == "中文.py"
    assert changed_file.new_path == "中文.py"


def test_git_c_style_escaped_quote_path_is_decoded() -> None:
    quoted_old = '"a/quote\\"name.py"'
    quoted_new = '"b/quote\\"name.py"'
    content = (
        f"diff --git {quoted_old} {quoted_new}\n"
        f"--- {quoted_old}\n"
        f"+++ {quoted_new}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    changed_file = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=content)
        .change_set.files[0]
    )

    assert changed_file.new_path == 'quote"name.py'


def test_git_c_style_path_is_validated_after_decoding() -> None:
    quoted_old = '"a/safe\\057..\\057private.py"'
    quoted_new = '"b/safe\\057..\\057private.py"'
    content = (
        f"diff --git {quoted_old} {quoted_new}\nold mode 100644\nnew mode 100755\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert "private.py" not in str(captured.value.to_dict())


def test_malformed_input_returns_safe_stable_error() -> None:
    content = "this is not a diff\nSIMULATED_PRIVATE_VALUE"

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"
    assert "SIMULATED_PRIVATE_VALUE" not in repr(captured.value)
    assert "SIMULATED_PRIVATE_VALUE" not in str(captured.value.to_dict())


def test_truncated_hunk_is_reported_as_incomplete_without_source_text() -> None:
    content = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-SIMULATED_PARTIAL_VALUE\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_incomplete"
    assert captured.value.__cause__ is None
    assert "SIMULATED_PARTIAL_VALUE" not in repr(captured.value)
    assert "SIMULATED_PARTIAL_VALUE" not in str(captured.value.to_dict())


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


def test_nonempty_hunk_range_requires_a_positive_start() -> None:
    content = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -0,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"


@pytest.mark.parametrize(
    "second_header",
    [
        "@@ -5,1 +5,1 @@",
        "@@ -11,1 +11,1 @@",
    ],
)
def test_hunks_must_be_ordered_and_nonoverlapping(second_header: str) -> None:
    content = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -10,3 +10,3 @@\n"
        " keep-10\n"
        "-old-11\n"
        "+new-11\n"
        " keep-12\n"
        f"{second_header}\n"
        "-old\n"
        "+new\n"
    )

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_malformed"


@pytest.mark.parametrize(
    "proof_updates",
    [
        {"input_identity_digest": "0" * 64},
        {"provider_id": "other"},
        {"provider_version": "2"},
        {"fixed_version": "f" * 64},
        {"byte_count": 1},
        {"file_count": 2},
        {"changed_line_count": 3},
        {"limits": InputLimits(max_files=199)},
        {"normalization_version": "other"},
    ],
)
def test_change_set_rejects_mismatched_completeness_proof(
    proof_updates: dict[str, object],
) -> None:
    change_set = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=fixture("basic.diff"))
        .change_set
    )
    forged_proof = replace(change_set.completeness, **cast(Any, proof_updates))

    with pytest.raises(ValueError):
        replace(change_set, completeness=forged_proof)


def test_change_set_normalization_must_also_match_input_identity() -> None:
    change_set = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=fixture("basic.diff"))
        .change_set
    )
    forged_proof = replace(change_set.completeness, normalization_version="v2")

    with pytest.raises(ValueError):
        replace(
            change_set,
            completeness=forged_proof,
            normalization_version="v2",
        )


def test_line_rejects_tampered_id_location_or_content_reference() -> None:
    result = make_service().normalize_plain_diff(
        task_id="task-1", text=fixture("basic.diff")
    )
    line = result.change_set.files[0].hunks[0].lines[1]

    with pytest.raises(ValueError):
        replace(line, line_id="line_" + "0" * 64)
    with pytest.raises(ValueError):
        replace(line, old_line_number=99)
    with pytest.raises(ValueError):
        replace(
            line,
            content_ref=replace(line.content_ref, start=line.content_ref.start + 1),
        )
    with pytest.raises(ValueError):
        replace(
            line,
            content_ref=replace(
                line.content_ref,
                artifact=replace(line.content_ref.artifact, artifact_id="forged"),
            ),
        )


def test_hunk_rejects_tampered_id_digest_or_range() -> None:
    hunk = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=fixture("basic.diff"))
        .change_set.files[0]
        .hunks[0]
    )

    with pytest.raises(ValueError):
        replace(hunk, hunk_id="hunk_" + "0" * 64)
    with pytest.raises(ValueError):
        replace(hunk, content_digest="0" * 64)
    with pytest.raises(ValueError):
        replace(hunk, old_start=hunk.old_start + 1)


@pytest.mark.parametrize(
    "updates",
    [
        {"file_id": "file_" + "0" * 64},
        {"old_path": "other.py"},
        {"language": "typescript"},
    ],
)
def test_changed_file_rejects_tampered_derived_fields(
    updates: dict[str, object],
) -> None:
    changed_file = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=fixture("basic.diff"))
        .change_set.files[0]
    )

    with pytest.raises(ValueError):
        replace(changed_file, **cast(Any, updates))


def test_change_set_recomputes_canonical_id_and_digest() -> None:
    change_set = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=fixture("basic.diff"))
        .change_set
    )
    forged_digest = "0" * 64
    forged_proof = replace(
        change_set.completeness,
        change_set_digest=forged_digest,
    )

    with pytest.raises(ValueError):
        replace(change_set, change_set_id="changeset_" + "0" * 64)
    with pytest.raises(ValueError):
        replace(
            change_set,
            completeness=forged_proof,
            change_set_digest=forged_digest,
        )


@pytest.mark.parametrize(
    "reference_updates",
    [
        {"artifact_id": "forged"},
        {"kind": ArtifactKind.REPORT_PAYLOAD},
        {"purpose": ArtifactPurpose.PERSISTENCE},
    ],
)
def test_change_set_rejects_tampered_sanitized_artifact_identity(
    reference_updates: dict[str, object],
) -> None:
    change_set = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=fixture("basic.diff"))
        .change_set
    )
    forged_reference = replace(
        change_set.sanitized_diff_ref,
        **cast(Any, reference_updates),
    )

    with pytest.raises(ValueError):
        replace(change_set, sanitized_diff_ref=forged_reference)


def test_change_set_coverage_must_exactly_classify_every_file() -> None:
    change_set = (
        make_service()
        .normalize_plain_diff(task_id="task-1", text=fixture("binary.diff"))
        .change_set
    )
    forged_coverage = replace(
        change_set.coverage,
        status=CoverageStatus.COMPLETE,
        reviewable_file_ids=(),
        unreviewable_file_ids=(),
    )

    with pytest.raises(ValueError):
        replace(change_set, coverage=forged_coverage)


@pytest.mark.parametrize(
    "binding_updates",
    [
        {"input_type": "github_pr"},
        {"object_identity": "forged"},
        {"changeset_ref": "forged"},
        {"base_sha": "a" * 40},
        {"head_sha": "b" * 40},
    ],
)
def test_normalized_input_rejects_mismatched_binding(
    binding_updates: dict[str, object],
) -> None:
    result = make_service().normalize_plain_diff(
        task_id="task-1", text=fixture("basic.diff")
    )
    forged_binding = replace(result.binding, **cast(Any, binding_updates))

    with pytest.raises(ValueError):
        NormalizedInput(change_set=result.change_set, binding=forged_binding)


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


def test_exactly_200_files_is_accepted() -> None:
    content = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n"
        f"--- a/f{i}.py\n"
        f"+++ b/f{i}.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        for i in range(200)
    )

    result = make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert result.change_set.file_count == 200


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


def test_changed_line_limit_stops_before_hashing_line_10001(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added = "".join(f"+new-{i}\n" for i in range(10_001))
    content = (
        "diff --git a/large.py b/large.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/large.py\n"
        "@@ -0,0 +1,10001 @@\n"
        f"{added}"
    )
    digest_calls = 0

    def counting_digest(value: bytes) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return sha256_bytes(value)

    monkeypatch.setattr(input_service_module, "sha256_bytes", counting_digest)

    with pytest.raises(StableError) as captured:
        make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert captured.value.code == "input_too_large"
    assert digest_calls == 10_000


def test_exactly_10000_changed_lines_is_accepted() -> None:
    deleted = "".join(f"-old-{i}\n" for i in range(5_000))
    added = "".join(f"+new-{i}\n" for i in range(5_000))
    content = (
        "diff --git a/large.py b/large.py\n"
        "--- a/large.py\n"
        "+++ b/large.py\n"
        "@@ -1,5000 +1,5000 @@\n"
        f"{deleted}{added}"
    )

    result = make_service().normalize_plain_diff(task_id="task-1", text=content)

    assert result.change_set.changed_line_count == 10_000


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
