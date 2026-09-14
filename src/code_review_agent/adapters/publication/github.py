"""Publish review comments to GitHub.com."""

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


class GitHubPublisher(GuardedPublisher):
    provider_id = "github"
    api_origin = "https://api.github.com"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["accept"] = "application/vnd.github+json"
        headers["x-github-api-version"] = "2022-11-28"
        return headers

    def _base(self, target: PublicationTarget) -> str:
        if target.platform != self.provider_id:
            raise PublicationError("publication_target_invalid")
        return f"{self.api_origin}/repos/{quote(target.repository, safe='/')}"

    def verify_target(self, target: PublicationTarget) -> RemoteTargetState:
        payload = self._json(self._get(f"{self._base(target)}/pulls/{target.number}"))
        try:
            return RemoteTargetState(
                open=payload["state"] == "open" and payload.get("merged_at") is None,
                head_sha=str(payload["head"]["sha"]),
            )
        except (KeyError, TypeError):
            raise PublicationError("publication_provider_response_invalid") from None

    def find_markers(
        self, target: PublicationTarget, markers: tuple[str, ...]
    ) -> dict[str, RemoteComment]:
        found: dict[str, RemoteComment] = {}
        urls = (
            f"{self._base(target)}/issues/{target.number}/comments?per_page=100",
            f"{self._base(target)}/pulls/{target.number}/comments?per_page=100",
        )
        for initial in urls:
            seen: set[str] = set()
            url: str | None = initial
            while url:
                self._guard_page(url, seen)
                response = self._get(url)
                payload = self._json(response)
                if not isinstance(payload, list):
                    raise PublicationError("publication_provider_response_invalid")
                self._collect(payload, markers, found)
                url = self._next_link(response)
        return found

    @staticmethod
    def _collect(
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
                        str(remote_id), str(raw.get("html_url") or "")
                    )

    def create_line_comment(
        self, target: PublicationTarget, item: PublicationItem
    ) -> RemoteComment:
        if item.position is None:
            raise PublicationError("publication_position_invalid")
        response = self._post(
            f"{self._base(target)}/pulls/{target.number}/comments",
            {
                "body": item.body,
                "commit_id": target.head_sha,
                "line": item.position.line,
                "path": item.position.path,
                "side": item.position.side,
            },
        )
        return self._comment(response)

    def create_summary_comment(
        self, target: PublicationTarget, item: PublicationItem
    ) -> RemoteComment:
        response = self._post(
            f"{self._base(target)}/issues/{target.number}/comments",
            {"body": item.body},
        )
        return self._comment(response)

    def _comment(self, response: object) -> RemoteComment:
        payload = self._json(response, write=True)  # type: ignore[arg-type]
        try:
            return RemoteComment(str(payload["id"]), str(payload["html_url"]))
        except (KeyError, TypeError):
            raise PublicationError(
                "publication_result_unknown", outcome_unknown=True
            ) from None
