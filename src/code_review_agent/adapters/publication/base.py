"""Guarded HTTP primitives shared by remote publishers."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from code_review_agent.adapters.input.remote import parse_link_next
from code_review_agent.domain.publication.models import PublicationError


class GuardedPublisher:
    provider_id: str
    api_origin: str
    max_pages = 10

    def __init__(
        self,
        credentials: Any,
        *,
        client: httpx.Client | None = None,
        max_response_bytes: int = 1_048_576,
    ) -> None:
        self._credentials = credentials
        self._client = client or httpx.Client(
            follow_redirects=False, trust_env=False, timeout=20.0
        )
        self._max_response_bytes = max_response_bytes

    def _headers(self) -> dict[str, str]:
        token = self._credentials.get(provider_id=self.provider_id, alias="shared")
        if not token:
            raise PublicationError("publication_credential_missing")
        return {
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }

    def _get(self, url: str) -> httpx.Response:
        self._validate_url(url)
        try:
            response = self._client.get(url, headers=self._headers())
        except httpx.HTTPError:
            raise PublicationError("publication_provider_unavailable") from None
        return self._validate_response(response, write=False)

    def _post(self, url: str, payload: dict[str, object]) -> httpx.Response:
        self._validate_url(url)
        try:
            response = self._client.post(
                url,
                headers=self._headers(),
                content=json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8"),
            )
        except httpx.HTTPError:
            raise PublicationError(
                "publication_result_unknown", outcome_unknown=True
            ) from None
        return self._validate_response(response, write=True)

    def _validate_response(
        self, response: httpx.Response, *, write: bool
    ) -> httpx.Response:
        if 300 <= response.status_code < 400:
            raise PublicationError("publication_provider_redirect_rejected")
        if response.status_code == 401:
            raise PublicationError("publication_credential_missing")
        if response.status_code == 403:
            raise PublicationError("publication_permission_denied")
        if response.status_code in (400, 404, 409, 422):
            raise PublicationError(
                "publication_position_invalid"
                if write
                else "publication_target_invalid"
            )
        if response.status_code >= 500:
            raise PublicationError(
                "publication_result_unknown"
                if write
                else "publication_provider_unavailable",
                outcome_unknown=write,
            )
        if response.status_code >= 400:
            raise PublicationError("publication_provider_request_failed")
        if len(response.content) > self._max_response_bytes:
            raise PublicationError(
                "publication_result_unknown"
                if write
                else "publication_provider_response_invalid",
                outcome_unknown=write,
            )
        return response

    def _json(self, response: httpx.Response, *, write: bool = False) -> Any:
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            raise PublicationError(
                "publication_result_unknown"
                if write
                else "publication_provider_response_invalid",
                outcome_unknown=write,
            ) from None

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        allowed = urlparse(self.api_origin)
        if (
            parsed.scheme != "https"
            or parsed.netloc != allowed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise PublicationError("publication_target_invalid")

    def _next_link(self, response: httpx.Response) -> str | None:
        try:
            return parse_link_next(
                response.headers.get("link"), allowed_origin=self.api_origin
            )
        except Exception:
            raise PublicationError("publication_provider_response_invalid") from None

    def _guard_page(self, url: str, seen: set[str]) -> None:
        if url in seen or len(seen) >= self.max_pages:
            raise PublicationError("publication_provider_response_invalid")
        seen.add(url)
