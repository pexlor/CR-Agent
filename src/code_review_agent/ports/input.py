"""Ports for fixed plain diff acquisition and security handling."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from code_review_agent.domain.input.models import AcquiredPlainDiff
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactPurpose,
    PreparedSanitizedArtifact,
    SanitizedArtifactRef,
)


@runtime_checkable
class InputProviderPort(Protocol):
    def acquire_text(self, *, task_id: str, content: str) -> AcquiredPlainDiff:
        """Fix and security-check an in-memory UTF-8 plain diff."""

    def acquire_file(self, *, task_id: str, path: Path) -> AcquiredPlainDiff:
        """Read, fix and security-check a UTF-8 plain diff file."""


class SecurityBoundaryPort(Protocol):
    def evaluate_artifact(
        self, content: str, descriptor: ArtifactDescriptor
    ) -> PreparedSanitizedArtifact:
        """Evaluate untrusted content without promoting it."""

    def commit(self, prepared: PreparedSanitizedArtifact) -> SanitizedArtifactRef:
        """Promote only an accepted sanitized artifact."""

    def resolve(
        self, reference: SanitizedArtifactRef, *, expected_purpose: ArtifactPurpose
    ) -> str:
        """Resolve committed sanitized content for its fixed purpose."""
