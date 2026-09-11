"""Immutable contracts for normalized code review input."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.common.time import ensure_utc
from code_review_agent.domain.security.models import (
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


@dataclass(frozen=True, slots=True)
class Hunk:
    hunk_id: str
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


@dataclass(frozen=True, slots=True)
class ChangedFile:
    file_id: str
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
            self.acquisition_attempts <= 0
            or min(self.byte_count, self.file_count, self.changed_line_count) < 0
        ):
            raise ValueError("completeness statistics must be valid")
        if not self.pagination_contiguous or not self.counts_match:
            raise ValueError("complete input must have consistent counts")
        if self.truncated or self.folded or self.overflowed or self.version_drifted:
            raise ValueError("incomplete input cannot be marked complete")
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
        if self.completeness.change_set_digest != self.change_set_digest:
            raise ValueError("completeness proof does not bind the change set")
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
