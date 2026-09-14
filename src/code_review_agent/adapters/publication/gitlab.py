"""Publish review comments to GitLab.com."""

from __future__ import annotations

from urllib.parse import quote

from code_review_agent.adapters.publication.base import GuardedPublisher
from code_review_agent.domain.publication.models import (
    PublicationError,
    PublicationItem,
    PublicationTarget,
    RemoteComment,
    RemoteTargetState,
)


class GitLabPublisher(GuardedPublisher):
    provider_id = "gitlab"
    api_origin = "https://gitlab.com"

    def _base(self, target: PublicationTarget) -> str:
        if target.platform != self.provider_id:
            raise PublicationError("publication_target_invalid")
        project = quote(target.repository, safe="")
        return (
            f"{self.api_origin}/api/v4/projects/{project}"
            f"/merge_requests/{target.number}"
        )

    def verify_target(self, target: PublicationTarget) -> RemoteTargetState:
        payload = self._json(self._get(self._base(target)))
        try:
            return RemoteTargetState(
                open=payload["state"] == "opened",
                head_sha=str(payload["diff_refs"]["head_sha"]),
            )
        except (KeyError, TypeError):
            raise PublicationError("publication_provider_response_invalid") from None

    def find_markers(
        self, target: PublicationTarget, markers: tuple[str, ...]
    ) -> dict[str, RemoteComment]:
        found: dict[str, RemoteComment] = {}
        for suffix in ("notes", "discussions"):
            seen: set[str] = set()
            url: str | None = f"{self._base(target)}/{suffix}?per_page=100"
            while url:
                self._guard_page(url, seen)
                response = self._get(url)
                payload = self._json(response)
                if not isinstance(payload, list):
                    raise PublicationError("publication_provider_response_invalid")
                notes = payload if suffix == "notes" else _discussion_notes(payload)
                _collect_notes(notes, markers, found)
                url = self._next_link(response)
        return found

    def create_line_comment(
        self, target: PublicationTarget, item: PublicationItem
    ) -> RemoteComment:
        if item.position is None:
            raise PublicationError("publication_position_invalid")
        position: dict[str, object] = {
            "base_sha": target.base_sha,
            "head_sha": target.head_sha,
            "new_path": item.position.new_path,
            "old_path": item.position.old_path,
            "position_type": "text",
            "start_sha": target.start_sha,
        }
        position["new_line" if item.position.side == "RIGHT" else "old_line"] = (
            item.position.line
        )
        response = self._post(
            f"{self._base(target)}/discussions",
            {"body": item.body, "position": position},
        )
        return self._comment(response)

    def create_summary_comment(
        self, target: PublicationTarget, item: PublicationItem
    ) -> RemoteComment:
        response = self._post(f"{self._base(target)}/notes", {"body": item.body})
        return self._comment(response)

    def _comment(self, response: object) -> RemoteComment:
        payload = self._json(response, write=True)  # type: ignore[arg-type]
        try:
            return RemoteComment(str(payload["id"]), str(payload.get("web_url") or ""))
        except (KeyError, TypeError):
            raise PublicationError(
                "publication_result_unknown", outcome_unknown=True
            ) from None


def _discussion_notes(payload: list[object]) -> list[object]:
    notes: list[object] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("notes"), list):
            raise PublicationError("publication_provider_response_invalid")
        notes.extend(item["notes"])
    return notes


def _collect_notes(
    payload: list[object],
    markers: tuple[str, ...],
    found: dict[str, RemoteComment],
) -> None:
    for raw in payload:
        if not isinstance(raw, dict) or not isinstance(raw.get("body"), str):
            raise PublicationError("publication_provider_response_invalid")
        for marker in markers:
            if marker in raw["body"]:
                remote_id = raw.get("id")
                if remote_id is None:
                    raise PublicationError("publication_provider_response_invalid")
                found[marker] = RemoteComment(
                    str(remote_id), str(raw.get("web_url") or "")
                )
