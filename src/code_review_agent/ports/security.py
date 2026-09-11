"""Ports used by the security domain."""

from __future__ import annotations

from typing import Protocol

from code_review_agent.domain.security.models import ArtifactDescriptor, ScanResult
from code_review_agent.domain.security.policy import SecurityPolicy


class SensitiveDataScannerPort(Protocol):
    """Fixed, non-networking scanner boundary."""

    def scan(
        self,
        content: str,
        descriptor: ArtifactDescriptor,
        policy: SecurityPolicy,
    ) -> ScanResult:
        """Return a complete structured scan or an indeterminate result."""
