"""Immutable contracts for normalized code review input."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.common.time import ensure_utc
from code_review_agent.domain.security.models import (
    ArtifactKind,
    ArtifactPurpose,
    SanitizedArtifactRef,
    SecurityDecision,
)
from code_review_agent.domain.task.models import InputBinding

SCHEMA_VERSION = "1.0"
PLAIN_DIFF_PROVIDER_ID = "plain_diff"
PLAIN_DIFF_PROVIDER_VERSION = "1"
NORMALIZATION_VERSION = "plain_diff_utf8_lf_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ChangeType(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    BINARY = "binary"


class LineType(StrEnum):
    CONTEXT = "context"
    ADDITION = "addition"
    DELETION = "deletion"


class CompletenessStatus(StrEnum):
    COMPLETE = "complete"


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class InputLimits:
    max_bytes: int = 1_048_576
    max_files: int = 200
    max_changed_lines: int = 10_000

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_files, self.max_changed_lines) <= 0:
            raise ValueError("input limits must be positive")


@dataclass(frozen=True, slots=True)
class InputIdentity:
    input_type: str
    provider_id: str
    provider_version: str
    schema_version: str
    content_digest: str
    digest_algorithm: str
    normalization_version: str
    identity_digest: str = field(init=False)
    display_name: str = field(init=False)

    def __post_init__(self) -> None:
        if self.input_type != PLAIN_DIFF_PROVIDER_ID:
            raise ValueError("unsupported input identity type")
        if not _SHA256.fullmatch(self.content_digest):
            raise ValueError("content digest must be lowercase SHA-256")
        if self.digest_algorithm != "sha256":
            raise ValueError("unsupported digest algorithm")
        digest = sha256_digest(
            {
                "input_type": self.input_type,
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "schema_version": self.schema_version,
                "content_digest": self.content_digest,
                "digest_algorithm": self.digest_algorithm,
                "normalization_version": self.normalization_version,
            }
        )
        object.__setattr__(self, "identity_digest", digest)
        object.__setattr__(self, "display_name", f"plain diff {digest[:12]}")

    @classmethod
    def for_plain_diff(cls, content_digest: str) -> InputIdentity:
        return cls(
            input_type=PLAIN_DIFF_PROVIDER_ID,
            provider_id=PLAIN_DIFF_PROVIDER_ID,
            provider_version=PLAIN_DIFF_PROVIDER_VERSION,
            schema_version=SCHEMA_VERSION,
            content_digest=content_digest,
            digest_algorithm="sha256",
            normalization_version=NORMALIZATION_VERSION,
        )


@dataclass(frozen=True, slots=True)
class AcquiredPlainDiff:
    """A fixed identity and committed safe reference, without source content."""

    identity: InputIdentity
    artifact_ref: SanitizedArtifactRef
    byte_count: int

    def __post_init__(self) -> None:
        if self.byte_count < 0:
            raise ValueError("byte count must be non-negative")


@dataclass(frozen=True, slots=True)
class ArtifactSliceRef:
    artifact: SanitizedArtifactRef
    start: int
    end: int
    content_digest: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("artifact slice must have valid offsets")
        if not _SHA256.fullmatch(self.content_digest):
            raise ValueError("slice digest must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class Line:
    line_id: str
    hunk_id: str
    line_type: LineType
    old_line_number: int | None
    new_line_number: int | None
    sequence: int
    content_ref: ArtifactSliceRef
    security_decision: SecurityDecision

    def __post_init__(self) -> None:
        if not self.line_id or self.sequence <= 0:
            raise ValueError("line identity and sequence are required")
        if self.line_type is LineType.ADDITION and (
            self.old_line_number is not None or self.new_line_number is None
        ):
            raise ValueError("addition must only have a new-side location")
        if self.line_type is LineType.DELETION and (
            self.old_line_number is None or self.new_line_number is not None
        ):
            raise ValueError("deletion must only have an old-side location")
        if self.line_type is LineType.CONTEXT and (
            self.old_line_number is None or self.new_line_number is None
        ):
            raise ValueError("context must have both side locations")
        expected_id = derive_line_id(
            hunk_id=self.hunk_id,
            line_type=self.line_type,
            old_line_number=self.old_line_number,
            new_line_number=self.new_line_number,
            sequence=self.sequence,
            content_ref=self.content_ref,
        )
        if self.line_id != expected_id:
            raise ValueError("line identity does not match canonical content")
        if self.security_decision is not self.content_ref.artifact.decision:
            raise ValueError("line security decision does not match its artifact")


@dataclass(frozen=True, slots=True)
class Hunk:
    hunk_id: str
    file_id: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[Line, ...]
    content_digest: str

    def __post_init__(self) -> None:
        if (
            not self.hunk_id
            or min(self.old_start, self.old_count, self.new_start, self.new_count) < 0
        ):
            raise ValueError("hunk identity and ranges must be valid")
        if not _SHA256.fullmatch(self.content_digest):
            raise ValueError("hunk digest must be lowercase SHA-256")
        if (self.old_count > 0 and self.old_start == 0) or (
            self.new_count > 0 and self.new_start == 0
        ):
            raise ValueError("non-empty hunk ranges require positive starts")
        old_line_count = sum(
            line.line_type in (LineType.CONTEXT, LineType.DELETION)
            for line in self.lines
        )
        new_line_count = sum(
            line.line_type in (LineType.CONTEXT, LineType.ADDITION)
            for line in self.lines
        )
        if old_line_count != self.old_count or new_line_count != self.new_count:
            raise ValueError("hunk line counts do not match its ranges")
        expected_digest = derive_hunk_content_digest(
            old_start=self.old_start,
            old_count=self.old_count,
            new_start=self.new_start,
            new_count=self.new_count,
            lines=self.lines,
        )
        expected_id = derive_hunk_id(
            file_id=self.file_id,
            old_start=self.old_start,
            old_count=self.old_count,
            new_start=self.new_start,
            new_count=self.new_count,
            content_digest=expected_digest,
        )
        if self.content_digest != expected_digest or self.hunk_id != expected_id:
            raise ValueError("hunk identity does not match canonical content")
        if any(line.hunk_id != self.hunk_id for line in self.lines):
            raise ValueError("hunk lines must bind to their parent hunk")
        _validate_line_locations(self)


@dataclass(frozen=True, slots=True)
class ChangedFile:
    file_id: str
    change_set_id: str
    old_path: str | None
    new_path: str | None
    change_type: ChangeType
    language: str | None
    is_binary: bool
    hunks: tuple[Hunk, ...]
    additions: int
    deletions: int
    unreviewable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.file_id or (self.old_path is None and self.new_path is None):
            raise ValueError("changed file identity and path are required")
        if min(self.additions, self.deletions) < 0:
            raise ValueError("file statistics must be non-negative")
        if self.is_binary != (self.change_type is ChangeType.BINARY):
            raise ValueError("binary flag and change type must agree")
        if self.is_binary and self.unreviewable_reason != "binary_content":
            raise ValueError("binary files must state why they are unreviewable")
        if self.is_binary and self.hunks:
            raise ValueError("binary files cannot contain text hunks")
        additions = sum(
            line.line_type is LineType.ADDITION
            for hunk in self.hunks
            for line in hunk.lines
        )
        deletions = sum(
            line.line_type is LineType.DELETION
            for hunk in self.hunks
            for line in hunk.lines
        )
        if self.additions != additions or self.deletions != deletions:
            raise ValueError("file statistics do not match hunk lines")
        expected_id = derive_file_id(
            change_set_id=self.change_set_id,
            change_type=self.change_type,
            old_path=self.old_path,
            new_path=self.new_path,
        )
        if self.file_id != expected_id:
            raise ValueError("file identity does not match canonical paths")
        if self.language != infer_language(
            self.new_path or self.old_path, self.is_binary
        ):
            raise ValueError("file language does not match its canonical path")
        if any(hunk.file_id != self.file_id for hunk in self.hunks):
            raise ValueError("file hunks must bind to their parent file")
        _validate_hunk_order(self.hunks)


@dataclass(frozen=True, slots=True)
class ScopeCoverage:
    status: CoverageStatus
    reviewable_file_ids: tuple[str, ...]
    unreviewable_file_ids: tuple[str, ...]
    safely_skipped_file_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        all_ids = (
            self.reviewable_file_ids
            + self.unreviewable_file_ids
            + self.safely_skipped_file_ids
        )
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("coverage scopes must not overlap")
        expected = (
            CoverageStatus.PARTIAL
            if self.unreviewable_file_ids or self.safely_skipped_file_ids
            else CoverageStatus.COMPLETE
        )
        if self.status is not expected:
            raise ValueError("coverage status does not match scope")


@dataclass(frozen=True, slots=True)
class CompletenessProof:
    status: CompletenessStatus
    input_identity_digest: str
    provider_id: str
    provider_version: str
    acquisition_attempts: int
    fixed_version: str
    byte_count: int
    file_count: int
    changed_line_count: int
    limits: InputLimits
    pagination_contiguous: bool
    counts_match: bool
    truncated: bool
    folded: bool
    overflowed: bool
    version_drifted: bool
    binary_file_ids: tuple[str, ...]
    change_set_digest: str
    normalization_version: str
    proof_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.status is not CompletenessStatus.COMPLETE:
            raise ValueError("only complete input can form a completeness proof")
        if (
            not _SHA256.fullmatch(self.input_identity_digest)
            or not _SHA256.fullmatch(self.fixed_version)
            or not _SHA256.fullmatch(self.change_set_digest)
        ):
            raise ValueError("completeness digests must be lowercase SHA-256")
        if not all(
            isinstance(value, str) and value
            for value in (
                self.provider_id,
                self.provider_version,
                self.normalization_version,
            )
        ):
            raise ValueError("completeness versions must be non-empty")
        if (
            self.acquisition_attempts <= 0
            or min(self.byte_count, self.file_count, self.changed_line_count) < 0
        ):
            raise ValueError("completeness statistics must be valid")
        if not self.pagination_contiguous or not self.counts_match:
            raise ValueError("complete input must have consistent counts")
        if self.truncated or self.folded or self.overflowed or self.version_drifted:
            raise ValueError("incomplete input cannot be marked complete")
        if (
            self.byte_count > self.limits.max_bytes
            or self.file_count > self.limits.max_files
            or self.changed_line_count > self.limits.max_changed_lines
        ):
            raise ValueError("completeness statistics exceed input limits")
        if len(set(self.binary_file_ids)) != len(self.binary_file_ids):
            raise ValueError("binary file identities must be unique")
        digest = sha256_digest(
            {
                "status": self.status.value,
                "input_identity_digest": self.input_identity_digest,
                "provider": [self.provider_id, self.provider_version],
                "acquisition_attempts": self.acquisition_attempts,
                "fixed_version": self.fixed_version,
                "statistics": [
                    self.byte_count,
                    self.file_count,
                    self.changed_line_count,
                ],
                "limits": [
                    self.limits.max_bytes,
                    self.limits.max_files,
                    self.limits.max_changed_lines,
                ],
                "checks": [
                    self.pagination_contiguous,
                    self.counts_match,
                    self.truncated,
                    self.folded,
                    self.overflowed,
                    self.version_drifted,
                ],
                "binary_file_ids": list(self.binary_file_ids),
                "change_set_digest": self.change_set_digest,
                "normalization_version": self.normalization_version,
            }
        )
        object.__setattr__(self, "proof_digest", digest)


@dataclass(frozen=True, slots=True)
class ChangeSet:
    change_set_id: str
    schema_version: str
    identity: InputIdentity
    completeness: CompletenessProof
    files: tuple[ChangedFile, ...]
    byte_count: int
    file_count: int
    changed_line_count: int
    limits: InputLimits
    coverage: ScopeCoverage
    sanitized_diff_ref: SanitizedArtifactRef
    normalization_version: str
    change_set_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.change_set_id or not _SHA256.fullmatch(self.change_set_digest):
            raise ValueError("change set identity and digest are required")
        if self.file_count != len(self.files):
            raise ValueError("change set file count mismatch")
        actual_changed_lines = sum(
            changed_file.additions + changed_file.deletions
            for changed_file in self.files
        )
        if self.changed_line_count != actual_changed_lines:
            raise ValueError("change set line count mismatch")
        if self.byte_count < 0:
            raise ValueError("change set byte count must be non-negative")
        expected_id = derive_change_set_id(self.identity)
        if self.change_set_id != expected_id:
            raise ValueError("change set identity does not match input identity")
        if self.completeness.change_set_digest != self.change_set_digest:
            raise ValueError("completeness proof does not bind the change set")
        proof = self.completeness
        if (
            proof.input_identity_digest != self.identity.identity_digest
            or proof.provider_id != self.identity.provider_id
            or proof.provider_version != self.identity.provider_version
            or proof.fixed_version != self.identity.content_digest
            or proof.normalization_version != self.normalization_version
            or self.normalization_version != self.identity.normalization_version
            or self.schema_version != self.identity.schema_version
        ):
            raise ValueError("completeness proof identity mismatch")
        if (
            proof.byte_count != self.byte_count
            or proof.file_count != self.file_count
            or proof.changed_line_count != self.changed_line_count
            or proof.limits != self.limits
        ):
            raise ValueError("completeness proof statistics mismatch")
        file_ids = tuple(changed_file.file_id for changed_file in self.files)
        if len(set(file_ids)) != len(file_ids):
            raise ValueError("change set file identities must be unique")
        if any(
            changed_file.change_set_id != self.change_set_id
            for changed_file in self.files
        ):
            raise ValueError("changed files must bind to their change set")
        covered_ids = (
            self.coverage.reviewable_file_ids
            + self.coverage.unreviewable_file_ids
            + self.coverage.safely_skipped_file_ids
        )
        if set(covered_ids) != set(file_ids):
            raise ValueError("coverage must classify every changed file exactly once")
        expected_unreviewable = {
            changed_file.file_id
            for changed_file in self.files
            if changed_file.is_binary
        }
        if set(self.coverage.unreviewable_file_ids) != expected_unreviewable:
            raise ValueError("coverage does not match unreviewable files")
        if set(proof.binary_file_ids) != expected_unreviewable:
            raise ValueError("completeness proof does not match binary files")
        expected_reviewable = (
            set(file_ids)
            - expected_unreviewable
            - set(self.coverage.safely_skipped_file_ids)
        )
        if set(self.coverage.reviewable_file_ids) != expected_reviewable:
            raise ValueError("coverage does not match reviewable files")
        reference = self.sanitized_diff_ref
        if (
            reference.kind is not ArtifactKind.DIFF
            or reference.purpose is not ArtifactPurpose.DOMAIN_INGRESS
            or reference.artifact_id != f"plain-diff-{self.identity.identity_digest}"
        ):
            raise ValueError("change set security artifact identity mismatch")
        if any(
            line.content_ref.artifact != reference
            for changed_file in self.files
            for hunk in changed_file.hunks
            for line in hunk.lines
        ):
            raise ValueError(
                "line content references must bind to the change set artifact"
            )
        expected_digest = derive_change_set_digest(
            change_set_id=self.change_set_id,
            schema_version=self.schema_version,
            identity=self.identity,
            files=self.files,
            byte_count=self.byte_count,
            changed_line_count=self.changed_line_count,
            limits=self.limits,
            coverage=self.coverage,
            sanitized_diff_ref=self.sanitized_diff_ref,
            normalization_version=self.normalization_version,
        )
        if self.change_set_digest != expected_digest:
            raise ValueError("change set digest does not match canonical content")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class NormalizedInput:
    change_set: ChangeSet
    binding: InputBinding

    def __post_init__(self) -> None:
        if self.binding.content_digest != self.change_set.identity.content_digest:
            raise ValueError("input binding content mismatch")
        if (
            self.binding.completeness_digest
            != self.change_set.completeness.proof_digest
        ):
            raise ValueError("input binding completeness mismatch")
        expected_ref = (
            f"{self.change_set.change_set_id}:{self.change_set.change_set_digest}"
        )
        if (
            self.binding.input_type != self.change_set.identity.input_type
            or self.binding.object_identity != self.change_set.identity.identity_digest
            or self.binding.changeset_ref != expected_ref
        ):
            raise ValueError("input binding identity mismatch")
        if self.binding.base_sha is not None or self.binding.head_sha is not None:
            raise ValueError("plain diff binding cannot contain commit SHAs")
        if self.change_set.sanitized_diff_ref.task_id != self.binding.task_id:
            raise ValueError("input binding task mismatch")


def derive_line_id(
    *,
    hunk_id: str,
    line_type: LineType,
    old_line_number: int | None,
    new_line_number: int | None,
    sequence: int,
    content_ref: ArtifactSliceRef,
) -> str:
    return "line_" + sha256_digest(
        {
            "hunk_id": hunk_id,
            "line_type": line_type.value,
            "old_line_number": old_line_number,
            "new_line_number": new_line_number,
            "sequence": sequence,
            "content_ref": {
                "artifact_id": content_ref.artifact.artifact_id,
                "kind": content_ref.artifact.kind.value,
                "purpose": content_ref.artifact.purpose.value,
                "sanitized_digest": content_ref.artifact.sanitized_digest,
                "policy_digest": content_ref.artifact.policy_digest,
                "start": content_ref.start,
                "end": content_ref.end,
                "content_digest": content_ref.content_digest,
            },
        }
    )


def derive_hunk_content_digest(
    *,
    old_start: int,
    old_count: int,
    new_start: int,
    new_count: int,
    lines: Sequence[Line],
) -> str:
    return derive_hunk_content_digest_from_values(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        line_values=[
            (
                line.line_type,
                line.old_line_number,
                line.new_line_number,
                line.sequence,
                line.content_ref.start,
                line.content_ref.end,
                line.content_ref.content_digest,
            )
            for line in lines
        ],
    )


def derive_hunk_content_digest_from_values(
    *,
    old_start: int,
    old_count: int,
    new_start: int,
    new_count: int,
    line_values: Sequence[tuple[LineType, int | None, int | None, int, int, int, str]],
) -> str:
    return sha256_digest(
        {
            "ranges": [old_start, old_count, new_start, new_count],
            "lines": [
                [
                    line_type.value,
                    old_line_number,
                    new_line_number,
                    sequence,
                    start,
                    end,
                    content_digest,
                ]
                for (
                    line_type,
                    old_line_number,
                    new_line_number,
                    sequence,
                    start,
                    end,
                    content_digest,
                ) in line_values
            ],
        }
    )


def derive_hunk_id(
    *,
    file_id: str,
    old_start: int,
    old_count: int,
    new_start: int,
    new_count: int,
    content_digest: str,
) -> str:
    return "hunk_" + sha256_digest(
        {
            "file_id": file_id,
            "ranges": [old_start, old_count, new_start, new_count],
            "content_digest": content_digest,
        }
    )


def derive_file_id(
    *,
    change_set_id: str,
    change_type: ChangeType,
    old_path: str | None,
    new_path: str | None,
) -> str:
    return "file_" + sha256_digest(
        {
            "change_set_id": change_set_id,
            "change_type": change_type.value,
            "old_path": old_path,
            "new_path": new_path,
        }
    )


def derive_change_set_id(identity: InputIdentity) -> str:
    return "changeset_" + sha256_digest(
        {
            "identity": identity.identity_digest,
            "normalization_version": identity.normalization_version,
            "schema_major": 1,
        }
    )


def derive_change_set_digest(
    *,
    change_set_id: str,
    schema_version: str,
    identity: InputIdentity,
    files: Sequence[ChangedFile],
    byte_count: int,
    changed_line_count: int,
    limits: InputLimits,
    coverage: ScopeCoverage,
    sanitized_diff_ref: SanitizedArtifactRef,
    normalization_version: str,
) -> str:
    return sha256_digest(
        {
            "change_set_id": change_set_id,
            "schema_version": schema_version,
            "identity": identity.identity_digest,
            "files": [_file_digest_payload(item) for item in files],
            "statistics": [byte_count, len(files), changed_line_count],
            "limits": [
                limits.max_bytes,
                limits.max_files,
                limits.max_changed_lines,
            ],
            "coverage": {
                "status": coverage.status.value,
                "reviewable": list(coverage.reviewable_file_ids),
                "unreviewable": list(coverage.unreviewable_file_ids),
                "safely_skipped": list(coverage.safely_skipped_file_ids),
            },
            "security": {
                "artifact_id": sanitized_diff_ref.artifact_id,
                "kind": sanitized_diff_ref.kind.value,
                "purpose": sanitized_diff_ref.purpose.value,
                "decision": sanitized_diff_ref.decision.value,
                "sanitized_digest": sanitized_diff_ref.sanitized_digest,
                "policy_id": sanitized_diff_ref.policy_id,
                "policy_version": sanitized_diff_ref.policy_version,
                "policy_digest": sanitized_diff_ref.policy_digest,
            },
            "normalization_version": normalization_version,
        }
    )


def infer_language(path: str | None, is_binary: bool) -> str | None:
    if path is None or is_binary:
        return None
    suffixes = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
    }
    return suffixes.get(PurePosixPath(path).suffix.lower())


def _file_digest_payload(changed_file: ChangedFile) -> dict[str, object]:
    return {
        "file_id": changed_file.file_id,
        "change_set_id": changed_file.change_set_id,
        "paths": [changed_file.old_path, changed_file.new_path],
        "change_type": changed_file.change_type.value,
        "language": changed_file.language,
        "binary": changed_file.is_binary,
        "statistics": [changed_file.additions, changed_file.deletions],
        "unreviewable_reason": changed_file.unreviewable_reason,
        "hunks": [
            {
                "hunk_id": hunk.hunk_id,
                "file_id": hunk.file_id,
                "ranges": [
                    hunk.old_start,
                    hunk.old_count,
                    hunk.new_start,
                    hunk.new_count,
                ],
                "content_digest": hunk.content_digest,
                "line_ids": [line.line_id for line in hunk.lines],
            }
            for hunk in changed_file.hunks
        ],
    }


def _validate_line_locations(hunk: Hunk) -> None:
    old_line = hunk.old_start
    new_line = hunk.new_start
    for sequence, line in enumerate(hunk.lines, start=1):
        if line.sequence != sequence:
            raise ValueError("hunk line sequence must be contiguous")
        if line.line_type is LineType.CONTEXT:
            if line.old_line_number != old_line or line.new_line_number != new_line:
                raise ValueError("context line location does not match hunk range")
            old_line += 1
            new_line += 1
        elif line.line_type is LineType.DELETION:
            if line.old_line_number != old_line:
                raise ValueError("deletion line location does not match hunk range")
            old_line += 1
        else:
            if line.new_line_number != new_line:
                raise ValueError("addition line location does not match hunk range")
            new_line += 1


def _validate_hunk_order(hunks: tuple[Hunk, ...]) -> None:
    previous: Hunk | None = None
    for hunk in hunks:
        if previous is not None:
            previous_old_end = previous.old_start + previous.old_count
            previous_new_end = previous.new_start + previous.new_count
            if (
                hunk.old_start < previous_old_end
                or hunk.new_start < previous_new_end
                or hunk.old_start < previous.old_start
                or hunk.new_start < previous.new_start
            ):
                raise ValueError("hunks must be ordered and non-overlapping")
        previous = hunk
