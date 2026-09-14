"""Deterministically turn an immutable review snapshot into publishable items."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.execution.execution_models import CandidateLocationKind
from code_review_agent.domain.publication.models import (
    PublicationItem,
    PublicationItemKind,
    PublicationPlan,
    PublicationPosition,
    PublicationTarget,
)


class PublicationPlanner:
    def plan(self, source_url: str, normalized: Any, snapshot: Any) -> PublicationPlan:
        identity = normalized.change_set.identity
        platform, repository, number = _parse_source_url(source_url)
        if (
            platform != identity.provider_id
            or repository != identity.repository_identity
            or number != identity.object_number
        ):
            raise ValueError("publication_target_mismatch")
        if not identity.base_sha or not identity.head_sha:
            raise ValueError("publication_revision_missing")
        target = PublicationTarget(
            platform=platform,
            source_url=source_url,
            repository=repository,
            number=number,
            base_sha=identity.base_sha,
            start_sha=getattr(identity, "start_sha", None) or identity.base_sha,
            head_sha=identity.head_sha,
        )
        publication_id = "publication_" + sha256_digest(
            {
                "task_id": snapshot.task_id,
                "snapshot_id": snapshot.snapshot_id,
                "target": [platform, repository, number],
                "revisions": [target.base_sha, target.start_sha, target.head_sha],
            }
        )
        files = {item.file_id: item for item in normalized.change_set.files}
        items: list[PublicationItem] = []
        summary_only: list[Any] = []
        for finding in snapshot.finding_set.findings:
            kind = finding.location.kind
            if kind not in (
                CandidateLocationKind.NEW_LINE,
                CandidateLocationKind.OLD_LINE,
            ):
                summary_only.append(finding)
                continue
            changed_file = files.get(finding.location.file_id)
            if changed_file is None or finding.location.line is None:
                raise ValueError("publication_position_invalid")
            side = "RIGHT" if kind is CandidateLocationKind.NEW_LINE else "LEFT"
            path = changed_file.new_path if side == "RIGHT" else changed_file.old_path
            if not path:
                raise ValueError("publication_position_invalid")
            if not _line_exists(changed_file, side=side, line=finding.location.line):
                raise ValueError("publication_position_invalid")
            position = PublicationPosition(
                path=path,
                old_path=changed_file.old_path or path,
                new_path=changed_file.new_path or path,
                line=finding.location.line,
                side=side,
            )
            visible = _render_finding(finding)
            items.append(
                _item(
                    publication_id,
                    PublicationItemKind.LINE,
                    visible,
                    finding_id=finding.finding_id,
                    fingerprint=finding.fingerprint,
                    position=position,
                )
            )
        summary = _render_summary(snapshot, summary_only)
        items.append(
            _item(
                publication_id,
                PublicationItemKind.SUMMARY,
                summary,
                finding_id=None,
                fingerprint="summary",
                position=None,
            )
        )
        return PublicationPlan(
            publication_id=publication_id,
            task_id=snapshot.task_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.snapshot_version,
            target=target,
            items=tuple(items),
        )


def _parse_source_url(source_url: str) -> tuple[str, str, int]:
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        raise ValueError("publication_target_invalid")
    parts = [unquote(item) for item in parsed.path.strip("/").split("/")]
    try:
        if parsed.netloc == "github.com" and len(parts) == 4 and parts[2] == "pull":
            return "github", "/".join(parts[:2]), int(parts[3])
        if parsed.netloc == "gitlab.com":
            marker = parts.index("merge_requests")
            if len(parts) == marker + 2:
                repository_parts = parts[:marker]
                if repository_parts and repository_parts[-1] == "-":
                    repository_parts = repository_parts[:-1]
                if len(repository_parts) >= 2:
                    return "gitlab", "/".join(repository_parts), int(parts[-1])
    except (ValueError, IndexError):
        pass
    raise ValueError("publication_target_invalid")


def _line_exists(changed_file: Any, *, side: str, line: int) -> bool:
    expected_type = "addition" if side == "RIGHT" else "deletion"
    number_field = "new_line_number" if side == "RIGHT" else "old_line_number"
    return any(
        getattr(candidate.line_type, "value", candidate.line_type) == expected_type
        and getattr(candidate, number_field) == line
        for hunk in changed_file.hunks
        for candidate in hunk.lines
    )


def _item(
    publication_id: str,
    kind: PublicationItemKind,
    visible_body: str,
    *,
    finding_id: str | None,
    fingerprint: str,
    position: PublicationPosition | None,
) -> PublicationItem:
    key = sha256_digest(
        {
            "publication_id": publication_id,
            "kind": kind.value,
            "finding": fingerprint,
            "position": None
            if position is None
            else [position.path, position.line, position.side],
            "visible_body": sha256_digest(visible_body),
        }
    )
    marker = f"<!-- cr-agent:publication:{key} -->"
    body = visible_body.rstrip() + f"\n\n{marker}"
    return PublicationItem(
        publication_key=key,
        kind=kind,
        body=body,
        body_digest=sha256_digest(body),
        marker=marker,
        finding_id=finding_id,
        position=position,
    )


def _render_finding(finding: Any) -> str:
    return "\n".join(
        (
            f"### {finding.title}",
            "",
            f"- Confidence: `{finding.confidence.value}`",
            f"- Problem: {finding.problem}",
            f"- Trigger: {finding.trigger_condition}",
            f"- Impact: {finding.impact}",
            f"- Suggestion: {finding.suggestion}",
            f"- Trace: `{finding.trace_id}`",
            f"- Limitations: {finding.limitations or 'None'}",
        )
    )


def _render_summary(snapshot: Any, summary_only: list[Any]) -> str:
    coverage = tuple(snapshot.finding_set.coverage.entries)
    budget = snapshot.budget_summary
    lines = [
        "## CR-Agent review summary",
        "",
        f"- Task: `{snapshot.task_id}`",
        f"- Snapshot: `{snapshot.snapshot_id}` (v{snapshot.snapshot_version})",
        f"- Result: `{snapshot.result_state}`",
        f"- Coverage: {len(coverage)} scope(s)",
        f"- Budget: {getattr(budget, 'known_consumption', 0)} / "
        f"{getattr(budget, 'authorized', 0)} tokens",
        f"- Trace events: {getattr(snapshot.trace_summary, 'event_count', 0)}",
    ]
    if snapshot.failure_stage:
        lines.append(f"- Limitation: {snapshot.failure_stage}")
    findings = tuple(snapshot.finding_set.findings)
    lines.extend(("", f"### Findings ({len(findings)})"))
    if not findings:
        lines.append("No valid findings.")
    else:
        summary_ids = {item.finding_id for item in summary_only}
        for finding in findings:
            location = finding.location
            suffix = " (summary only)" if finding.finding_id in summary_ids else ""
            place = location.file_id
            if location.line is not None:
                place += f":{location.line}"
            lines.append(f"- **{finding.title}** — `{place}`{suffix}")
        for finding in summary_only:
            lines.extend(("", _render_finding(finding)))
    return "\n".join(lines)
