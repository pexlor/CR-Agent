"""GitLab merge request input provider."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from code_review_agent.adapters.input.remote import (
    RemoteDiffProvider,
    RemoteRequest,
    _error,
    parse_link_next,
    parse_url,
)
from code_review_agent.domain.input.models import AcquiredPlainDiff
from code_review_agent.ports.credentials import CredentialStorePort
from code_review_agent.ports.input import SecurityBoundaryPort


class GitLabInputProvider(RemoteDiffProvider):
    def __init__(
        self,
        security: SecurityBoundaryPort,
        credentials: CredentialStorePort,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            provider_id="gitlab",
            provider_version="1",
            security=security,
            credentials=credentials,
            client=client,
        )

    def acquire_url(self, *, task_id: str, source_url: str) -> AcquiredPlainDiff:
        repository, number = parse_url(source_url, marker="merge_requests")
        parsed = httpx.URL(source_url)
        if parsed.host != "gitlab.com":
            raise _error("invalid_source_url")
        token = self._credentials.get(provider_id="gitlab", alias="shared")
        project = quote(repository, safe="")
        base_url = (
            f"https://gitlab.com/api/v4/projects/{project}/merge_requests/{number}"
        )
        metadata = self._request(base_url, token=token).json()
        if metadata.get("state") != "opened":
            raise _error("change_request_closed")
        diff_url: str | None = base_url + "/diffs?per_page=100"
        files: list[dict[str, object]] = []
        while diff_url:
            page = self._request(diff_url, token=token)
            payload = page.json()
            # The real GitLab.com REST API returns a bare JSON array of diff
            # entries from this endpoint. Some GitLab API responses elsewhere
            # wrap paginated results in an object with a nested list plus an
            # "overflow" flag; accept that shape too in case a self-hosted or
            # future GitLab version wraps it, but the plain list is what the
            # live API actually returns today.
            if isinstance(payload, list):
                values: object = payload
                overflow = False
            elif isinstance(payload, dict):
                values = payload.get("diffs")
                overflow = bool(payload.get("overflow"))
            else:
                raise _error("input_incomplete")
            if not isinstance(values, list) or overflow:
                raise _error("input_incomplete")
            files.extend(values)
            diff_url = parse_link_next(
                page.headers.get("link"), allowed_origin="https://gitlab.com"
            )
        return self._commit(
            task_id=task_id,
            request=RemoteRequest(
                "gitlab",
                "1",
                "https://gitlab.com",
                repository,
                number,
                metadata["diff_refs"]["base_sha"],
                metadata["diff_refs"]["head_sha"],
                _gitlab_diff(files),
            ),
        )


def _gitlab_diff(files: list[dict[str, object]]) -> str:
    chunks: list[str] = []
    for item in files:
        old = item.get("old_path")
        new = item.get("new_path")
        diff = item.get("diff")
        if (
            not isinstance(old, str)
            or not isinstance(new, str)
            or not isinstance(diff, str)
        ):
            raise _error("input_incomplete")
        if item.get("collapsed") or item.get("too_large"):
            raise _error("input_incomplete")
        # GitLab's `diff` field already ends with its own trailing newline;
        # appending another one here produces a spurious blank physical line
        # that the unified-diff parser rejects as malformed.
        chunks.append(
            f"diff --git a/{old} b/{new}\n--- a/{old}\n+++ b/{new}\n{diff}"
        )
    return "".join(chunks)
