"""GitHub pull request input provider."""

from __future__ import annotations

import json
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


class GitHubInputProvider(RemoteDiffProvider):
    def __init__(
        self,
        security: SecurityBoundaryPort,
        credentials: CredentialStorePort,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            provider_id="github",
            provider_version="1",
            security=security,
            credentials=credentials,
            client=client,
        )

    def acquire_url(self, *, task_id: str, source_url: str) -> AcquiredPlainDiff:
        repository, number = parse_url(source_url, marker="pull")
        parsed = httpx.URL(source_url)
        if parsed.host != "github.com":
            raise _error("invalid_source_url")
        token = self._credentials.get(provider_id="github", alias="shared")
        response = self._request(
            (
                "https://api.github.com/repos/"
                f"{quote(repository, safe='/')}/pulls/{number}"
            ),
            token=token,
        )
        try:
            metadata = response.json()
            if metadata.get("state") != "open" or metadata.get("merged_at") is not None:
                raise _error("change_request_closed")
            base = metadata["base"]["sha"]
            head = metadata["head"]["sha"]
            files_url = metadata["url"] + "/files?per_page=100"
            files = []
            while files_url:
                page = self._request(files_url, token=token)
                values = page.json()
                if not isinstance(values, list):
                    raise ValueError
                files.extend(values)
                files_url = parse_link_next(
                    page.headers.get("link"), allowed_origin="https://api.github.com"
                )
            if any(item.get("patch") is None for item in files):
                diff = self._request_text(
                    (
                        "https://api.github.com/repos/"
                        f"{quote(repository, safe='/')}/pulls/{number}"
                    ),
                    token=token,
                    accept="application/vnd.github.v3.diff",
                )
            else:
                diff = _github_diff(files)
            return self._commit(
                task_id=task_id,
                request=RemoteRequest(
                    "github",
                    "1",
                    "https://github.com",
                    repository,
                    number,
                    base,
                    head,
                    diff,
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise _error("provider_response_invalid") from None


def _github_diff(files: list[dict[str, object]]) -> str:
    chunks: list[str] = []
    for item in files:
        filename = item.get("filename")
        if not isinstance(filename, str):
            raise ValueError
        status = item.get("status")
        previous = item.get("previous_filename")
        old_name = previous if isinstance(previous, str) else filename
        patch = item.get("patch")
        if isinstance(patch, str):
            old_marker = "/dev/null" if status == "added" else f"a/{old_name}"
            new_marker = "/dev/null" if status == "removed" else f"b/{filename}"
            chunks.append(
                f"diff --git a/{old_name} b/{filename}\n"
                f"--- {old_marker}\n+++ {new_marker}\n{patch}\n"
            )
        elif status == "renamed" and isinstance(previous, str):
            chunks.append(
                f"diff --git a/{previous} b/{filename}\n"
                "similarity index 100%\n"
                f"rename from {previous}\nrename to {filename}\n"
            )
        else:
            old_marker = "/dev/null" if status == "added" else f"a/{old_name}"
            new_marker = "/dev/null" if status == "removed" else f"b/{filename}"
            chunks.append(
                f"diff --git a/{old_name} b/{filename}\n"
                f"Binary files {old_marker} and {new_marker} differ\n"
            )
    return "".join(chunks)
