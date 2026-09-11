"""Local UTF-8 plain diff provider."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

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
from code_review_agent.ports.input import SecurityBoundaryPort


class PlainDiffProvider:
    def __init__(
        self,
        security: SecurityBoundaryPort,
        *,
        limits: InputLimits | None = None,
    ) -> None:
        self._security = security
        self._limits = limits or InputLimits()

    def acquire_text(self, *, task_id: str, content: str) -> AcquiredPlainDiff:
        if not isinstance(content, str):
            raise _error("input_invalid_utf8", "input_acquisition")
        encoded: bytes | None
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            encoded = None
        if encoded is None:
            raise _error("input_invalid_utf8", "input_acquisition")
        return self._acquire(task_id=task_id, encoded=encoded)

    def acquire_file(self, *, task_id: str, path: Path) -> AcquiredPlainDiff:
        encoded: bytes | None
        try:
            with Path(path).open("rb") as stream:
                encoded = stream.read(self._limits.max_bytes + 1)
        except (OSError, TypeError, ValueError):
            encoded = None
        if encoded is None:
            raise _error("input_unreadable", "input_acquisition")
        return self._acquire(task_id=task_id, encoded=encoded)

    def _acquire(self, *, task_id: str, encoded: bytes) -> AcquiredPlainDiff:
        if len(encoded) > self._limits.max_bytes:
            raise _error(
                "input_too_large",
                "input_acquisition",
                details={"metric": "bytes", "limit": self._limits.max_bytes},
            )
        content: str | None
        try:
            content = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            content = None
        if content is None:
            raise _error("input_invalid_utf8", "input_acquisition")

        normalized = _normalize_text(content)
        content_digest = sha256_bytes(normalized.encode("utf-8"))
        identity = InputIdentity.for_plain_diff(content_digest)
        descriptor = ArtifactDescriptor(
            artifact_id=f"plain-diff-{identity.identity_digest}",
            task_id=task_id,
            source=ArtifactSource.USER_CLI,
            trust_label=TrustLabel.UNTRUSTED_TEXT,
            kind=ArtifactKind.DIFF,
            purpose=ArtifactPurpose.DOMAIN_INGRESS,
            max_size=self._limits.max_bytes,
        )
        prepared = None
        with suppress(Exception):
            prepared = self._security.evaluate_artifact(normalized, descriptor)
        if prepared is None:
            raise _error("security_boundary_failed", "input_acquisition")
        if prepared.decision not in (
            SecurityDecision.SAFE,
            SecurityDecision.REDACTED,
        ):
            raise _error("security_boundary_failed", "input_acquisition")
        reference = None
        with suppress(Exception):
            reference = self._security.commit(prepared)
        if reference is None:
            raise _error("security_boundary_failed", "input_acquisition")
        return AcquiredPlainDiff(
            identity=identity,
            artifact_ref=reference,
            byte_count=len(encoded),
        )


def _normalize_text(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _error(
    code: str,
    stage: str,
    *,
    details: dict[str, str | int] | None = None,
) -> StableError:
    return StableError(
        code=code,
        category="input",
        stage=stage,
        recoverable=True,
        next_actions=("correct_input",),
        details=details,
    )
