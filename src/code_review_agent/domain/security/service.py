"""Security evaluation, staging, promotion and resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import uuid4

from code_review_agent.domain.common.digests import sha256_bytes
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactPurpose,
    PreparedSanitizedArtifact,
    SanitizedArtifactRef,
    ScanResult,
    SecurityDecision,
    SecurityFinding,
    SensitiveCategory,
)
from code_review_agent.domain.security.policy import SecurityPolicy
from code_review_agent.ports.security import SensitiveDataScannerPort

_PLACEHOLDER_PREFIX: Final = "<REDACTED:"
_CATEGORY_PRIORITY: Final = {
    SensitiveCategory.PRIVATE_KEY: 0,
    SensitiveCategory.CERTIFICATE_MATERIAL: 0,
    SensitiveCategory.CREDENTIAL: 0,
    SensitiveCategory.CLOUD_SECRET: 0,
    SensitiveCategory.CONNECTION_STRING: 1,
    SensitiveCategory.BUSINESS_SENSITIVE: 2,
    SensitiveCategory.PERSONAL_DATA: 3,
    SensitiveCategory.UNKNOWN_SENSITIVE: 4,
}


@dataclass(frozen=True, slots=True)
class _CommittedArtifact:
    reference: SanitizedArtifactRef
    payload: str


class SecurityService:
    """Pure security boundary with an in-memory commit journal for the MVP."""

    def __init__(
        self, scanner: SensitiveDataScannerPort, *, policy: SecurityPolicy
    ) -> None:
        self._scanner = scanner
        self._policy = policy
        self._committed: dict[str, _CommittedArtifact] = {}

    def evaluate_artifact(
        self,
        content: str,
        descriptor: ArtifactDescriptor,
    ) -> PreparedSanitizedArtifact:
        if not isinstance(content, str):
            raise TypeError("artifact content must be text")
        if (
            descriptor.max_size is not None
            and len(content.encode("utf-8")) > descriptor.max_size
        ):
            return self._prepared(
                descriptor,
                SecurityDecision.BLOCKED,
                content,
                "",
                (),
            )
        try:
            scan = self._scanner.scan(content, descriptor, self._policy)
        except Exception:
            scan = ScanResult.indeterminate("scanner_unavailable")
        if not scan.complete_scan:
            return self._prepared(
                descriptor, SecurityDecision.INDETERMINATE, content, "", ()
            )
        if tuple(scan.detector_manifest) != tuple(self._policy.detector_manifest):
            return self._prepared(
                descriptor, SecurityDecision.INDETERMINATE, content, "", ()
            )
        try:
            self._validate_findings(scan.findings, len(content))
        except (TypeError, ValueError):
            return self._prepared(
                descriptor, SecurityDecision.INDETERMINATE, content, "", ()
            )

        actions = [
            self._policy.action_matrix.get(finding.category)
            for finding in scan.findings
        ]
        if any(action is None for action in actions):
            return self._prepared(
                descriptor, SecurityDecision.INDETERMINATE, content, "", ()
            )
        if any(action is SecurityDecision.BLOCKED for action in actions):
            return self._prepared(
                descriptor,
                SecurityDecision.BLOCKED,
                content,
                "",
                tuple(
                    sorted(
                        {finding.category for finding in scan.findings},
                        key=lambda item: item.value,
                    )
                ),
            )

        sanitized = content
        categories: set[SensitiveCategory] = set()
        if any(action is SecurityDecision.REDACTED for action in actions):
            sanitized, categories = self._redact(content, scan.findings)
            decision = SecurityDecision.REDACTED
        else:
            decision = SecurityDecision.SAFE
        return self._prepared(
            descriptor,
            decision,
            content,
            sanitized,
            tuple(sorted(categories, key=lambda item: item.value)),
        )

    def commit(self, prepared: PreparedSanitizedArtifact) -> SanitizedArtifactRef:
        if prepared.decision not in (SecurityDecision.SAFE, SecurityDecision.REDACTED):
            raise RuntimeError("only safe or redacted artifacts can be committed")
        if prepared.attestation_id in self._committed:
            existing = self._committed[prepared.attestation_id].reference
            if existing.sanitized_digest != prepared.sanitized_digest:
                raise ValueError("attestation mismatch")
            return existing
        reference = SanitizedArtifactRef(
            attestation_id=prepared.attestation_id,
            artifact_id=prepared.artifact_id,
            task_id=prepared.descriptor.task_id,
            source=prepared.descriptor.source,
            kind=prepared.descriptor.kind,
            purpose=prepared.descriptor.purpose,
            decision=prepared.decision,
            sanitized_digest=prepared.sanitized_digest,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.policy_version,
            policy_digest=self._policy.policy_digest,
            provenance=prepared.provenance,
        )
        self._committed[prepared.attestation_id] = _CommittedArtifact(
            reference, prepared.sanitized_payload
        )
        return reference

    def resolve(
        self, reference: SanitizedArtifactRef, *, expected_purpose: ArtifactPurpose
    ) -> str:
        committed = self._committed.get(reference.attestation_id)
        if committed is None or committed.reference != reference:
            raise ValueError("attestation mismatch")
        if reference.purpose is not expected_purpose:
            raise ValueError("artifact purpose mismatch")
        if reference.decision not in (SecurityDecision.SAFE, SecurityDecision.REDACTED):
            raise ValueError("artifact decision is not readable")
        if (
            reference.policy_id != self._policy.policy_id
            or reference.policy_digest != self._policy.policy_digest
        ):
            raise ValueError("security policy incompatible")
        if (
            sha256_bytes(committed.payload.encode("utf-8"))
            != reference.sanitized_digest
        ):
            raise ValueError("artifact digest mismatch")
        return committed.payload

    def _prepared(
        self,
        descriptor: ArtifactDescriptor,
        decision: SecurityDecision,
        original_payload: str,
        payload: str,
        categories: tuple[SensitiveCategory, ...],
    ) -> PreparedSanitizedArtifact:
        return PreparedSanitizedArtifact(
            attestation_id=str(uuid4()),
            artifact_id=descriptor.artifact_id,
            descriptor=descriptor,
            decision=decision,
            sanitized_payload=payload,
            original_digest=sha256_bytes(original_payload.encode("utf-8"))
            if original_payload
            else sha256_bytes(b""),
            sanitized_digest=sha256_bytes(payload.encode("utf-8")),
            categories=categories,
            provenance=descriptor.provenance,
        )

    @staticmethod
    def _validate_findings(
        findings: tuple[SecurityFinding, ...], content_length: int
    ) -> None:
        for finding in findings:
            if finding.end > content_length or finding.crosses_boundary:
                raise ValueError("finding range is invalid")

    @staticmethod
    def _redact(
        content: str,
        findings: tuple[SecurityFinding, ...],
    ) -> tuple[str, set[SensitiveCategory]]:
        range_values: set[tuple[int, int, SensitiveCategory]] = set()
        for finding in findings:
            matched = content[finding.start : finding.end]
            if not matched:
                continue
            search_from = 0
            while True:
                match_start = content.find(matched, search_from)
                if match_start < 0:
                    break
                range_values.add(
                    (match_start, match_start + len(matched), finding.category)
                )
                search_from = match_start + len(matched)
        ranges = sorted(
            range_values, key=lambda item: (item[0], item[1], item[2].value)
        )
        merged: list[tuple[int, int, SensitiveCategory]] = []
        for start, end, category in ranges:
            if merged and start <= merged[-1][1]:
                previous_start, previous_end, previous_category = merged[-1]
                merged[-1] = (
                    previous_start,
                    max(previous_end, end),
                    previous_category
                    if _CATEGORY_PRIORITY[previous_category]
                    <= _CATEGORY_PRIORITY[category]
                    else category,
                )
            else:
                merged.append((start, end, category))
        chunks: list[str] = []
        cursor = 0
        categories: set[SensitiveCategory] = set()
        for index, (start, end, category) in enumerate(merged, start=1):
            placeholder = f"{_PLACEHOLDER_PREFIX}{category.value.upper()}:{index:02d}>"
            chunks.append(content[cursor:start])
            chunks.append(placeholder)
            cursor = end
            categories.add(category)
        chunks.append(content[cursor:])
        return "".join(chunks), categories

    @property
    def policy(self) -> SecurityPolicy:
        return self._policy
