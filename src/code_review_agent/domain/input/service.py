"""Strict normalization of a committed, sanitized unified diff."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from code_review_agent.domain.common.digests import sha256_bytes, sha256_digest
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.common.time import Clock
from code_review_agent.domain.input.models import (
    SCHEMA_VERSION,
    AcquiredPlainDiff,
    ArtifactSliceRef,
    ChangedFile,
    ChangeSet,
    ChangeType,
    CompletenessProof,
    CompletenessStatus,
    CoverageStatus,
    Hunk,
    InputLimits,
    Line,
    LineType,
    NormalizedInput,
    ScopeCoverage,
    derive_change_set_digest,
    derive_change_set_id,
    derive_file_id,
    derive_hunk_content_digest_from_values,
    derive_hunk_id,
    derive_line_id,
    infer_language,
)
from code_review_agent.domain.security.models import ArtifactPurpose
from code_review_agent.domain.task.models import InputBinding
from code_review_agent.ports.input import InputProviderPort, SecurityBoundaryPort

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_GIT_FILE_MODE = re.compile(r"^(?:100644|100755|120000|160000)$")
_SIMILARITY = re.compile(r"^(similarity|dissimilarity) index (0|[1-9][0-9]?|100)%$")
_INDEX = re.compile(
    r"^index ([0-9a-f]{4,64})\.\.([0-9a-f]{4,64})"
    r"(?: (100644|100755|120000|160000))?$"
)
_NO_NEWLINE_MARKER = "\\ No newline at end of file"
_C_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "t": b"\t",
    "n": b"\n",
    "v": b"\v",
    "f": b"\f",
    "r": b"\r",
    "\\": b"\\",
    '"': b'"',
}


class InputService:
    def __init__(
        self,
        provider: InputProviderPort,
        security: SecurityBoundaryPort,
        *,
        clock: Clock,
        limits: InputLimits | None = None,
    ) -> None:
        self._provider = provider
        self._security = security
        self._clock = clock
        self._limits = limits or InputLimits()

    def normalize_plain_diff(
        self,
        *,
        task_id: str,
        text: str | None = None,
        file_path: Path | None = None,
    ) -> NormalizedInput:
        if not task_id:
            raise ValueError("task_id must be non-empty")
        if text is None and file_path is None:
            raise _error("input_missing")
        if text is not None and file_path is not None:
            raise _error("input_multiple_sources")

        acquired = (
            self._provider.acquire_text(task_id=task_id, content=text)
            if text is not None
            else self._provider.acquire_file(task_id=task_id, path=file_path)  # type: ignore[arg-type]
        )
        sanitized: str | None = None
        with suppress(RuntimeError, TypeError, ValueError):
            sanitized = self._security.resolve(
                acquired.artifact_ref,
                expected_purpose=ArtifactPurpose.DOMAIN_INGRESS,
            )
        if sanitized is None:
            raise _error("security_boundary_failed")

        result: NormalizedInput | None = None
        failure: StableError | None = None
        try:
            result = self._normalize_committed(
                task_id=task_id,
                acquired=acquired,
                sanitized=sanitized,
            )
        except _TooLarge as exc:
            failure = _error(
                "input_too_large",
                details={"metric": exc.metric, "limit": exc.limit},
            )
        except _IncompleteDiff:
            failure = _error("input_incomplete")
        except (_MalformedDiff, ValueError):
            failure = _error("input_malformed")
        if failure is not None:
            raise failure
        if result is None:
            raise RuntimeError("input normalization produced no result")
        return result

    def normalize_remote(self, *, task_id: str, source_url: str) -> NormalizedInput:
        acquire = getattr(self._provider, "acquire_url", None)
        if acquire is None:
            raise _error("url_provider_not_configured")
        acquired = acquire(task_id=task_id, source_url=source_url)
        sanitized: str | None = None
        with suppress(RuntimeError, TypeError, ValueError):
            sanitized = self._security.resolve(
                acquired.artifact_ref,
                expected_purpose=ArtifactPurpose.DOMAIN_INGRESS,
            )
        if sanitized is None:
            raise _error("security_boundary_failed")
        try:
            return self._normalize_committed(
                task_id=task_id, acquired=acquired, sanitized=sanitized
            )
        except _TooLarge as exc:
            raise _error(
                "input_too_large",
                details={"metric": exc.metric, "limit": exc.limit},
            ) from None
        except _IncompleteDiff:
            raise _error("input_incomplete") from None
        except (_MalformedDiff, ValueError):
            raise _error("input_malformed") from None

    def _normalize_committed(
        self,
        *,
        task_id: str,
        acquired: AcquiredPlainDiff,
        sanitized: str,
    ) -> NormalizedInput:
        change_set_id = derive_change_set_id(acquired.identity)
        files = _UnifiedDiffParser(
            content=sanitized,
            acquired=acquired,
            limits=self._limits,
            change_set_id=change_set_id,
        ).parse()

        changed_line_count = sum(item.additions + item.deletions for item in files)
        reviewable = tuple(item.file_id for item in files if not item.is_binary)
        unreviewable = tuple(item.file_id for item in files if item.is_binary)
        coverage = ScopeCoverage(
            status=(
                CoverageStatus.PARTIAL if unreviewable else CoverageStatus.COMPLETE
            ),
            reviewable_file_ids=reviewable,
            unreviewable_file_ids=unreviewable,
        )
        change_set_digest = derive_change_set_digest(
            change_set_id=change_set_id,
            task_id=task_id,
            schema_version=SCHEMA_VERSION,
            identity=acquired.identity,
            files=files,
            byte_count=acquired.byte_count,
            changed_line_count=changed_line_count,
            limits=self._limits,
            coverage=coverage,
            sanitized_diff_ref=acquired.artifact_ref,
            normalization_version=acquired.identity.normalization_version,
        )
        completeness = CompletenessProof(
            status=CompletenessStatus.COMPLETE,
            input_identity_digest=acquired.identity.identity_digest,
            provider_id=acquired.identity.provider_id,
            provider_version=acquired.identity.provider_version,
            acquisition_attempts=1,
            fixed_version=(
                acquired.identity.head_sha or acquired.identity.content_digest
            ),
            byte_count=acquired.byte_count,
            file_count=len(files),
            changed_line_count=changed_line_count,
            limits=self._limits,
            pagination_contiguous=True,
            counts_match=True,
            truncated=False,
            folded=False,
            overflowed=False,
            version_drifted=False,
            binary_file_ids=unreviewable,
            change_set_digest=change_set_digest,
            normalization_version=acquired.identity.normalization_version,
        )
        change_set = ChangeSet(
            change_set_id=change_set_id,
            task_id=task_id,
            schema_version=SCHEMA_VERSION,
            identity=acquired.identity,
            completeness=completeness,
            files=files,
            byte_count=acquired.byte_count,
            file_count=len(files),
            changed_line_count=changed_line_count,
            limits=self._limits,
            coverage=coverage,
            sanitized_diff_ref=acquired.artifact_ref,
            normalization_version=acquired.identity.normalization_version,
            change_set_digest=change_set_digest,
            created_at=self._clock.now(),
        )
        binding = InputBinding(
            binding_id="binding_"
            + sha256_digest(
                {
                    "task_id": task_id,
                    "identity": acquired.identity.identity_digest,
                    "change_set_digest": change_set_digest,
                    "completeness_digest": completeness.proof_digest,
                }
            ),
            task_id=task_id,
            input_type=acquired.identity.input_type,
            object_identity=acquired.identity.identity_digest,
            base_sha=acquired.identity.base_sha,
            head_sha=acquired.identity.head_sha,
            content_digest=acquired.identity.content_digest,
            completeness_digest=completeness.proof_digest,
            changeset_ref=f"{change_set_id}:{change_set_digest}",
        )
        return NormalizedInput(change_set=change_set, binding=binding)


@dataclass(frozen=True, slots=True)
class _PhysicalLine:
    body: str
    start: int


@dataclass(frozen=True, slots=True)
class _LineDraft:
    line_type: LineType
    old_line_number: int | None
    new_line_number: int | None
    sequence: int
    content_start: int
    content_end: int
    content_digest: str


class _UnifiedDiffParser:
    def __init__(
        self,
        *,
        content: str,
        acquired: AcquiredPlainDiff,
        limits: InputLimits,
        change_set_id: str,
    ) -> None:
        self._content = content
        self._acquired = acquired
        self._limits = limits
        self._change_set_id = change_set_id

    def parse(self) -> tuple[ChangedFile, ...]:
        if self._content == "":
            return ()
        lines = _physical_lines(self._content, self._limits.max_physical_lines)
        if not lines or not lines[0].body.startswith("diff --git "):
            raise _MalformedDiff
        sections: list[tuple[_PhysicalLine, ...]] = []
        start = 0
        for index in range(1, len(lines)):
            if lines[index].body.startswith("diff --git "):
                sections.append(tuple(lines[start:index]))
                start = index
        sections.append(tuple(lines[start:]))
        if len(sections) > self._limits.max_files:
            raise _TooLarge("files", self._limits.max_files)

        files: list[ChangedFile] = []
        seen_file_ids: set[str] = set()
        changed_line_count = 0
        for section in sections:
            changed_file = self._parse_file(
                section,
                remaining_changed_lines=(
                    self._limits.max_changed_lines - changed_line_count
                ),
            )
            changed_line_count += changed_file.additions + changed_file.deletions
            if changed_line_count > self._limits.max_changed_lines:
                raise _TooLarge("changed_lines", self._limits.max_changed_lines)
            if changed_file.file_id in seen_file_ids:
                raise _MalformedDiff
            seen_file_ids.add(changed_file.file_id)
            files.append(changed_file)
        return tuple(files)

    def _parse_file(
        self,
        lines: tuple[_PhysicalLine, ...],
        *,
        remaining_changed_lines: int,
    ) -> ChangedFile:
        header_old, header_new = _parse_diff_header(
            lines[0].body,
            hints=_header_path_hints(lines),
        )
        marker_old: str | None = header_old
        marker_new: str | None = header_new
        has_markers = False
        binary = False
        binary_paths: tuple[str | None, str | None] | None = None
        renamed = header_old != header_new
        new_file = False
        deleted_file = False
        recognized_fact = False
        rename_old: str | None = None
        rename_new: str | None = None
        similarity: tuple[str, int] | None = None
        old_mode: str | None = None
        new_mode: str | None = None
        index_metadata: tuple[str, str, str | None] | None = None
        hunk_drafts: list[tuple[int, int, int, int, tuple[_LineDraft, ...], str]] = []
        parsed_changed_lines = 0

        index = 1
        while index < len(lines):
            body = lines[index].body
            if body.startswith("@@"):
                draft, index, hunk_changed_lines = self._parse_hunk(
                    lines,
                    index,
                    remaining_changed_lines=(
                        remaining_changed_lines - parsed_changed_lines
                    ),
                )
                hunk_drafts.append(draft)
                parsed_changed_lines += hunk_changed_lines
                recognized_fact = True
                continue
            if body.startswith("--- "):
                if (
                    has_markers
                    or index + 1 >= len(lines)
                    or not lines[index + 1].body.startswith("+++ ")
                ):
                    raise _MalformedDiff
                marker_old = _parse_marker_path(lines[index].body[4:], "a/")
                marker_new = _parse_marker_path(lines[index + 1].body[4:], "b/")
                has_markers = True
                recognized_fact = True
                index += 2
                continue
            if body.startswith("rename from "):
                if rename_old is not None:
                    raise _MalformedDiff
                rename_old = _normalize_path(
                    _decode_git_path_field(body.removeprefix("rename from ")),
                    None,
                )
                renamed = True
                recognized_fact = True
            elif body.startswith("rename to "):
                if rename_new is not None:
                    raise _MalformedDiff
                rename_new = _normalize_path(
                    _decode_git_path_field(body.removeprefix("rename to ")),
                    None,
                )
                renamed = True
                recognized_fact = True
            elif body.startswith("similarity index ") or body.startswith(
                "dissimilarity index "
            ):
                match = _SIMILARITY.fullmatch(body)
                if match is None or similarity is not None:
                    raise _MalformedDiff
                similarity = (match.group(1), int(match.group(2)))
                recognized_fact = True
            elif body.startswith("new file mode "):
                if new_file or not _GIT_FILE_MODE.fullmatch(
                    body.removeprefix("new file mode ")
                ):
                    raise _MalformedDiff
                new_file = True
                recognized_fact = True
            elif body.startswith("deleted file mode "):
                if deleted_file or not _GIT_FILE_MODE.fullmatch(
                    body.removeprefix("deleted file mode ")
                ):
                    raise _MalformedDiff
                deleted_file = True
                recognized_fact = True
            elif body.startswith("old mode "):
                mode = body.removeprefix("old mode ")
                if old_mode is not None or not _GIT_FILE_MODE.fullmatch(mode):
                    raise _MalformedDiff
                old_mode = mode
                recognized_fact = True
            elif body.startswith("new mode "):
                mode = body.removeprefix("new mode ")
                if new_mode is not None or not _GIT_FILE_MODE.fullmatch(mode):
                    raise _MalformedDiff
                new_mode = mode
                recognized_fact = True
            elif body.startswith("index "):
                match = _INDEX.fullmatch(body)
                if match is None or index_metadata is not None:
                    raise _MalformedDiff
                index_metadata = (match.group(1), match.group(2), match.group(3))
                recognized_fact = True
            elif body.startswith("Binary files ") and body.endswith(" differ"):
                if binary_paths is not None:
                    raise _MalformedDiff
                binary_paths = _parse_binary_paths(body, header_old, header_new)
                binary = True
                recognized_fact = True
            elif body == "GIT binary patch":
                raise _MalformedDiff
            elif body == _NO_NEWLINE_MARKER:
                if not hunk_drafts:
                    raise _MalformedDiff
            else:
                raise _MalformedDiff
            index += 1

        if not recognized_fact:
            raise _MalformedDiff
        if new_file and deleted_file:
            raise _MalformedDiff
        if (new_file or deleted_file) and renamed:
            raise _MalformedDiff
        if (old_mode is None) != (new_mode is None):
            raise _MalformedDiff
        if old_mode is not None and (old_mode == new_mode or new_file or deleted_file):
            raise _MalformedDiff
        if (
            index_metadata is not None
            and index_metadata[2] is not None
            and (old_mode is not None or new_file or deleted_file)
        ):
            raise _MalformedDiff
        if index_metadata is not None:
            old_is_zero = set(index_metadata[0]) == {"0"}
            new_is_zero = set(index_metadata[1]) == {"0"}
            if old_is_zero != new_file or new_is_zero != deleted_file:
                raise _MalformedDiff
        similarity_kind = similarity[0] if similarity is not None else None
        similarity_percent = similarity[1] if similarity is not None else None
        if similarity_kind == "similarity" and (
            rename_old is None or rename_new is None
        ):
            raise _MalformedDiff
        if similarity_kind == "dissimilarity" and (
            rename_old is not None or rename_new is not None
        ):
            raise _MalformedDiff
        if binary_paths is not None:
            binary_old, binary_new = binary_paths
            if has_markers and (marker_old != binary_old or marker_new != binary_new):
                raise _MalformedDiff
            if binary_old is not None and binary_old != header_old:
                raise _MalformedDiff
            if binary_new is not None and binary_new != header_new:
                raise _MalformedDiff
            if (new_file and binary_old is not None) or (
                deleted_file and binary_new is not None
            ):
                raise _MalformedDiff
            if (new_file and binary_new is None) or (
                deleted_file and binary_old is None
            ):
                raise _MalformedDiff
            if (binary_old is None and not new_file) or (
                binary_new is None and not deleted_file
            ):
                raise _MalformedDiff
            marker_old, marker_new = binary_paths
        if new_file:
            if has_markers and marker_old is not None:
                raise _MalformedDiff
            marker_old = None
        if deleted_file:
            if has_markers and marker_new is not None:
                raise _MalformedDiff
            marker_new = None
        if rename_old is not None and rename_old != header_old:
            raise _MalformedDiff
        if rename_new is not None and rename_new != header_new:
            raise _MalformedDiff
        if renamed and (rename_old is None) != (rename_new is None):
            raise _MalformedDiff
        if has_markers:
            if marker_old is not None and marker_old != header_old:
                raise _MalformedDiff
            if marker_new is not None and marker_new != header_new:
                raise _MalformedDiff
        if hunk_drafts and not has_markers:
            raise _MalformedDiff
        if binary and hunk_drafts:
            raise _MalformedDiff
        has_content = bool(hunk_drafts or binary)
        if similarity_kind == "dissimilarity" and not has_content:
            raise _MalformedDiff
        if similarity_kind == "similarity" and (
            (similarity_percent == 100 and has_content)
            or (similarity_percent != 100 and not has_content)
        ):
            raise _MalformedDiff
        if index_metadata is not None:
            old_hash, new_hash, _ = index_metadata
            hashes_differ = old_hash != new_hash
            lifecycle_only = (new_file or deleted_file) and not has_content
            if not has_content and not lifecycle_only:
                raise _MalformedDiff
            if not hashes_differ and has_content:
                raise _MalformedDiff

        if binary:
            change_type = ChangeType.BINARY
        elif marker_old is None:
            change_type = ChangeType.ADDED
        elif marker_new is None:
            change_type = ChangeType.DELETED
        elif renamed:
            change_type = ChangeType.RENAMED
        else:
            change_type = ChangeType.MODIFIED
        file_id = derive_file_id(
            change_set_id=self._change_set_id,
            change_type=change_type,
            old_path=marker_old,
            new_path=marker_new,
        )
        hunks = tuple(
            self._materialize_hunk(file_id=file_id, draft=draft)
            for draft in hunk_drafts
        )
        additions = sum(
            1
            for hunk in hunks
            for line in hunk.lines
            if line.line_type is LineType.ADDITION
        )
        deletions = sum(
            1
            for hunk in hunks
            for line in hunk.lines
            if line.line_type is LineType.DELETION
        )
        return ChangedFile(
            file_id=file_id,
            change_set_id=self._change_set_id,
            old_path=marker_old,
            new_path=marker_new,
            change_type=change_type,
            language=infer_language(marker_new or marker_old, binary),
            is_binary=binary,
            hunks=hunks,
            additions=additions,
            deletions=deletions,
            unreviewable_reason="binary_content" if binary else None,
        )

    def _parse_hunk(
        self,
        lines: tuple[_PhysicalLine, ...],
        index: int,
        *,
        remaining_changed_lines: int,
    ) -> tuple[tuple[int, int, int, int, tuple[_LineDraft, ...], str], int, int]:
        match = _HUNK_HEADER.fullmatch(lines[index].body)
        if match is None:
            raise _MalformedDiff
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        if (old_count > 0 and old_start == 0) or (new_count > 0 and new_start == 0):
            raise _MalformedDiff
        old_seen = 0
        new_seen = 0
        old_line = old_start
        new_line = new_start
        sequence = 0
        changed_lines = 0
        drafts: list[_LineDraft] = []
        index += 1

        while old_seen < old_count or new_seen < new_count:
            if index >= len(lines):
                raise _IncompleteDiff
            physical = lines[index]
            if physical.body == _NO_NEWLINE_MARKER:
                if not drafts:
                    raise _MalformedDiff
                index += 1
                continue
            if not physical.body or physical.body[0] not in (" ", "+", "-"):
                raise _MalformedDiff
            prefix = physical.body[0]
            content = physical.body[1:]
            sequence += 1
            if prefix == " ":
                if old_seen >= old_count or new_seen >= new_count:
                    raise _MalformedDiff
                line_type = LineType.CONTEXT
                old_number: int | None = old_line
                new_number: int | None = new_line
                old_seen += 1
                new_seen += 1
                old_line += 1
                new_line += 1
            elif prefix == "-":
                if changed_lines >= remaining_changed_lines:
                    raise _TooLarge("changed_lines", self._limits.max_changed_lines)
                changed_lines += 1
                if old_seen >= old_count:
                    raise _MalformedDiff
                line_type = LineType.DELETION
                old_number = old_line
                new_number = None
                old_seen += 1
                old_line += 1
            else:
                if changed_lines >= remaining_changed_lines:
                    raise _TooLarge("changed_lines", self._limits.max_changed_lines)
                changed_lines += 1
                if new_seen >= new_count:
                    raise _MalformedDiff
                line_type = LineType.ADDITION
                old_number = None
                new_number = new_line
                new_seen += 1
                new_line += 1
            drafts.append(
                _LineDraft(
                    line_type=line_type,
                    old_line_number=old_number,
                    new_line_number=new_number,
                    sequence=sequence,
                    content_start=physical.start + 1,
                    content_end=physical.start + 1 + len(content),
                    content_digest=sha256_bytes(content.encode("utf-8")),
                )
            )
            index += 1
        if index < len(lines) and lines[index].body == _NO_NEWLINE_MARKER:
            index += 1
        digest = derive_hunk_content_digest_from_values(
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            line_values=[
                (
                    draft.line_type,
                    draft.old_line_number,
                    draft.new_line_number,
                    draft.sequence,
                    draft.content_start,
                    draft.content_end,
                    draft.content_digest,
                )
                for draft in drafts
            ],
        )
        return (
            (
                old_start,
                old_count,
                new_start,
                new_count,
                tuple(drafts),
                digest,
            ),
            index,
            changed_lines,
        )

    def _materialize_hunk(
        self,
        *,
        file_id: str,
        draft: tuple[int, int, int, int, tuple[_LineDraft, ...], str],
    ) -> Hunk:
        old_start, old_count, new_start, new_count, line_drafts, digest = draft
        hunk_id = derive_hunk_id(
            file_id=file_id,
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            content_digest=digest,
        )
        materialized_lines: list[Line] = []
        for item in line_drafts:
            content_ref = ArtifactSliceRef(
                artifact=self._acquired.artifact_ref,
                start=item.content_start,
                end=item.content_end,
                content_digest=item.content_digest,
            )
            materialized_lines.append(
                Line(
                    line_id=derive_line_id(
                        hunk_id=hunk_id,
                        line_type=item.line_type,
                        old_line_number=item.old_line_number,
                        new_line_number=item.new_line_number,
                        sequence=item.sequence,
                        content_ref=content_ref,
                    ),
                    hunk_id=hunk_id,
                    line_type=item.line_type,
                    old_line_number=item.old_line_number,
                    new_line_number=item.new_line_number,
                    sequence=item.sequence,
                    content_ref=content_ref,
                    security_decision=self._acquired.artifact_ref.decision,
                )
            )
        lines = tuple(materialized_lines)
        return Hunk(
            hunk_id=hunk_id,
            file_id=file_id,
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            lines=lines,
            content_digest=digest,
        )


class _MalformedDiff(Exception):
    pass


class _IncompleteDiff(Exception):
    pass


class _TooLarge(Exception):
    def __init__(self, metric: str, limit: int) -> None:
        self.metric = metric
        self.limit = limit


def _physical_lines(content: str, max_lines: int) -> tuple[_PhysicalLine, ...]:
    values: list[_PhysicalLine] = []
    offset = 0
    while offset < len(content):
        if len(values) >= max_lines:
            raise _TooLarge("physical_lines", max_lines)
        newline = content.find("\n", offset)
        if newline < 0:
            body = content[offset:]
            next_offset = len(content)
        else:
            body = content[offset:newline]
            next_offset = newline + 1
        values.append(_PhysicalLine(body=body, start=offset))
        offset = next_offset
    return tuple(values)


def _header_path_hints(
    lines: tuple[_PhysicalLine, ...],
) -> tuple[str, str] | None:
    rename_old: str | None = None
    rename_new: str | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.body.startswith("@@"):
            break
        if line.body.startswith("--- ") and index + 1 < len(lines):
            next_body = lines[index + 1].body
            if next_body.startswith("+++ "):
                old_path = _parse_marker_path(line.body[4:], "a/")
                new_path = _parse_marker_path(next_body[4:], "b/")
                if old_path is None and new_path is not None:
                    return new_path, new_path
                if new_path is None and old_path is not None:
                    return old_path, old_path
                if old_path is not None and new_path is not None:
                    return old_path, new_path
        elif line.body.startswith("rename from ") and rename_old is None:
            rename_old = _normalize_path(
                _decode_git_path_field(line.body.removeprefix("rename from ")),
                None,
            )
        elif line.body.startswith("rename to ") and rename_new is None:
            rename_new = _normalize_path(
                _decode_git_path_field(line.body.removeprefix("rename to ")),
                None,
            )
    if rename_old is not None and rename_new is not None:
        return rename_old, rename_new
    return None


def _parse_diff_header(
    header: str,
    *,
    hints: tuple[str, str] | None = None,
) -> tuple[str, str]:
    prefix = "diff --git "
    if not header.startswith(prefix):
        raise _MalformedDiff
    payload = header[len(prefix) :]
    if payload.startswith('"'):
        old_value, consumed = _consume_c_style_path(payload)
        if consumed >= len(payload) or payload[consumed] != " ":
            raise _MalformedDiff
        new_value = _decode_git_path_field(payload[consumed + 1 :])
        return _normalize_path(old_value, "a/"), _normalize_path(new_value, "b/")
    if hints is not None:
        expected = f"a/{hints[0]} b/{hints[1]}"
        if payload != expected:
            raise _MalformedDiff
        return hints
    symmetric_length, remainder = divmod(len(payload) - 5, 2)
    if remainder == 0 and symmetric_length >= 0:
        separator = 2 + symmetric_length
        if payload[separator : separator + 3] == " b/":
            old_value = payload[:separator]
            new_value = payload[separator + 1 :]
            old_path = _normalize_path(old_value, "a/")
            new_path = _normalize_path(new_value, "b/")
            if old_path == new_path:
                return old_path, new_path
    first = payload.find(" b/", 2)
    if first < 0 or first != payload.rfind(" b/"):
        raise _MalformedDiff
    return (
        _normalize_path(payload[:first], "a/"),
        _normalize_path(payload[first + 1 :], "b/"),
    )


def _parse_marker_path(value: str, prefix: str) -> str | None:
    if value.startswith('"'):
        token, consumed = _consume_c_style_path(value)
        remainder = value[consumed:]
        if remainder and not remainder.startswith("\t"):
            raise _MalformedDiff
    else:
        token = value.split("\t", 1)[0]
    if token == "/dev/null":
        return None
    return _normalize_path(token, prefix)


def _parse_binary_paths(
    value: str,
    header_old: str,
    header_new: str,
) -> tuple[str | None, str | None]:
    pair = value.removeprefix("Binary files ").removesuffix(" differ")
    candidates = (
        (f"a/{header_old}", f"b/{header_new}", header_old, header_new),
        ("/dev/null", f"b/{header_new}", None, header_new),
        (f"a/{header_old}", "/dev/null", header_old, None),
    )
    matches = [
        (old_path, new_path)
        for old_value, new_value, old_path, new_path in candidates
        if pair == f"{old_value} and {new_value}"
    ]
    if len(matches) == 1:
        return matches[0]
    if pair.startswith('"'):
        old_value, consumed = _consume_c_style_path(pair)
        if pair[consumed : consumed + 5] != " and ":
            raise _MalformedDiff
        new_value = _decode_git_path_field(pair[consumed + 5 :])
        old_path = _normalize_path(old_value, "a/")
        new_path = _normalize_path(new_value, "b/")
        if old_path == header_old and new_path == header_new:
            return old_path, new_path
    raise _MalformedDiff


def _decode_git_path_field(value: str) -> str:
    if not value:
        raise _MalformedDiff
    if not value.startswith('"'):
        return value
    decoded, consumed = _consume_c_style_path(value)
    if consumed != len(value):
        raise _MalformedDiff
    return decoded


def _consume_c_style_path(value: str) -> tuple[str, int]:
    if not value.startswith('"'):
        raise _MalformedDiff
    decoded = bytearray()
    index = 1
    while index < len(value):
        character = value[index]
        if character == '"':
            try:
                return decoded.decode("utf-8", errors="strict"), index + 1
            except UnicodeDecodeError:
                raise _MalformedDiff from None
        if character != "\\":
            decoded.extend(character.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise _MalformedDiff
        escaped = value[index]
        if escaped in "01234567":
            end = index + 1
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            octet = int(value[index:end], 8)
            if octet > 255:
                raise _MalformedDiff
            decoded.append(octet)
            index = end
            continue
        replacement = _C_ESCAPES.get(escaped)
        if replacement is None:
            raise _MalformedDiff
        decoded.extend(replacement)
        index += 1
    raise _MalformedDiff


def _normalize_path(value: str, prefix: str | None) -> str:
    if (
        not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _MalformedDiff
    if prefix is not None:
        if not value.startswith(prefix):
            raise _MalformedDiff
        value = value[len(prefix) :]
    if (
        not value
        or value.startswith("/")
        or _WINDOWS_DRIVE.match(value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise _MalformedDiff
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise _MalformedDiff
    return value


def _error(
    code: str,
    *,
    details: dict[str, str | int] | None = None,
) -> StableError:
    return StableError(
        code=code,
        category="input",
        stage="input_normalization",
        recoverable=True,
        next_actions=("correct_input",),
        details=details,
    )
