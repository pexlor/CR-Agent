"""Shared guarded HTTP acquisition for hosted change requests."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from code_review_agent.domain.common.digests import sha256_bytes
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.input.models import (
    AcquiredPlainDiff,
    InputIdentity,
    InputLimits,
)
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    SecurityDecision,
    TrustLabel,
)
from code_review_agent.ports.credentials import CredentialStorePort
from code_review_agent.ports.input import SecurityBoundaryPort


@dataclass(frozen=True, slots=True)
class RemoteRequest:
    provider_id: str
    provider_version: str
    api_origin: str
    repository_identity: str
    object_number: int
    base_sha: str
    head_sha: str
    diff: str


class RemoteDiffProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        provider_version: str,
        security: SecurityBoundaryPort,
        credentials: CredentialStorePort,
        limits: InputLimits | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.provider_version = provider_version
        self._security = security
        self._credentials = credentials
        self._limits = limits or InputLimits()
        self._client = client or httpx.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=20.0,
        )

    def _request(
        self,
        url: str,
        *,
        token: str | None,
        accept: str = "application/json",
    ) -> httpx.Response:
        headers = {"accept": accept}
        if token:
            headers["authorization"] = f"Bearer {token}"
        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError:
            raise _error("provider_unavailable") from None
        if 300 <= response.status_code < 400:
            raise _error("provider_redirect_rejected")
        if response.status_code in (401, 403):
            code = (
                "credential_invalid"
                if response.status_code == 401
                else "permission_denied"
            )
            raise _error(code)
        if response.status_code == 404:
            raise _error("change_request_not_found")
        if response.status_code >= 400:
            raise _error("provider_request_failed")
        if len(response.content) > self._limits.max_bytes:
            raise _error("input_too_large")
        return response

    def _request_text(
        self, url: str, *, token: str | None, accept: str = "text/plain"
    ) -> str:
        response = self._request(url, token=token, accept=accept)
        try:
            return response.content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _error("provider_response_invalid") from None

    def _commit(self, *, task_id: str, request: RemoteRequest) -> AcquiredPlainDiff:
        normalized = request.diff.replace("\r\n", "\n").replace("\r", "\n")
        encoded = normalized.encode("utf-8")
        if len(encoded) > self._limits.max_bytes:
            raise _error("input_too_large")
        identity = InputIdentity.for_remote_change(
            provider_id=request.provider_id,
            provider_version=request.provider_version,
            repository_identity=request.repository_identity,
            object_number=request.object_number,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
            content_digest=sha256_bytes(encoded),
        )
        descriptor = ArtifactDescriptor(
            artifact_id=f"{request.provider_id}-diff-{identity.identity_digest}",
            task_id=task_id,
            source=ArtifactSource.PLATFORM_RESPONSE,
            trust_label=TrustLabel.UNTRUSTED_TEXT,
            kind=ArtifactKind.DIFF,
            purpose=ArtifactPurpose.DOMAIN_INGRESS,
            provenance=(request.api_origin,),
            max_size=self._limits.max_bytes,
        )
        try:
            prepared = self._security.evaluate_artifact(normalized, descriptor)
            if prepared.decision not in (
                SecurityDecision.SAFE,
                SecurityDecision.REDACTED,
            ):
                raise ValueError
            reference = self._security.commit(prepared)
        except Exception:
            raise _error("security_boundary_failed") from None
        return AcquiredPlainDiff(identity, reference, len(encoded))


def parse_link_next(value: str | None, *, allowed_origin: str) -> str | None:
    if not value:
        return None
    for item in value.split(","):
        part = item.strip()
        if part.endswith('rel="next"'):
            candidate = part[1 : part.find(">")]
            parsed = urlparse(candidate)
            expected = urlparse(allowed_origin)
            if (
                parsed.scheme != "https"
                or parsed.hostname != expected.hostname
                or parsed.port != expected.port
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise _error("provider_response_invalid")
            return candidate
    return None


def parse_url(url: str, *, marker: str) -> tuple[str, int]:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _error("invalid_source_url")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[-2] != marker or not parts[-1].isdigit():
        raise _error("invalid_source_url")
    return "/".join(parts[:-2]), int(parts[-1])


def _error(code: str) -> StableError:
    return StableError(
        code=code,
        category="input",
        stage="remote_acquisition",
        recoverable=True,
        next_actions=("correct_input",),
    )
