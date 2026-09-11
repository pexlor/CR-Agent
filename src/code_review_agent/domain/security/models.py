"""Pure value objects for the security boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from code_review_agent.domain.common.time import ensure_utc


class SecurityDecision(StrEnum):
    SAFE = "safe"
    REDACTED = "redacted"
    BLOCKED = "blocked"
    INDETERMINATE = "indeterminate"


class SensitiveCategory(StrEnum):
    CREDENTIAL = "credential"
    PRIVATE_KEY = "private_key"
    CERTIFICATE_MATERIAL = "certificate_material"
    CONNECTION_STRING = "connection_string"
    CLOUD_SECRET = "cloud_secret"
    PERSONAL_DATA = "personal_data"
    BUSINESS_SENSITIVE = "business_sensitive"
    UNKNOWN_SENSITIVE = "unknown_sensitive"


class ArtifactSource(StrEnum):
    USER_CLI = "user_cli"
    REPOSITORY = "repository"
    PLATFORM_RESPONSE = "platform_response"
    TOOL_OUTPUT = "tool_output"
    MODEL_RESPONSE = "model_response"
    TRUSTED_APPLICATION = "trusted_application"


class TrustLabel(StrEnum):
    TRUSTED_CONTROL_SCALAR = "trusted_control_scalar"
    TRUSTED_GENERATED_METADATA = "trusted_generated_metadata"
    UNTRUSTED_TEXT = "untrusted_text"


class ArtifactKind(StrEnum):
    DIFF = "diff"
    REPOSITORY_TEXT = "repository_text"
    PATH = "path"
    PROMPT = "prompt"
    MODEL_PAYLOAD = "model_payload"
    TOOL_PAYLOAD = "tool_payload"
    TRACE_PAYLOAD = "trace_payload"
    CHECKPOINT_PAYLOAD = "checkpoint_payload"
    ERROR_PAYLOAD = "error_payload"
    REPORT_PAYLOAD = "report_payload"
    LOG_PAYLOAD = "log_payload"


class ArtifactPurpose(StrEnum):
    DOMAIN_INGRESS = "domain_ingress"
    MODEL_EGRESS = "model_egress"
    TOOL_INGRESS = "tool_ingress"
    PERSISTENCE = "persistence"
    REPORT_RENDERING = "report_rendering"
    REPORT_DELIVERY = "report_delivery"
    USER_DISPLAY = "user_display"
    SECURITY_LOG = "security_log"
    TRACE_INPUT_SNAPSHOT = "trace_input_snapshot"
    TRACE_MODEL_REQUEST = "trace_model_request"
    TRACE_MODEL_RESPONSE = "trace_model_response"
    TRACE_TOOL_INPUT = "trace_tool_input"
    TRACE_TOOL_RESULT = "trace_tool_result"
    TRACE_ERROR_SUMMARY = "trace_error_summary"
    TRACE_FINDING_EVIDENCE = "trace_finding_evidence"
    TRACE_COMMENT_SUMMARY = "trace_comment_summary"
    TRACE_REPORT_SUMMARY = "trace_report_summary"


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """Immutable identity and security context for one artifact."""

    artifact_id: str
    task_id: str | None
    source: ArtifactSource
    trust_label: TrustLabel
    kind: ArtifactKind
    purpose: ArtifactPurpose
    provenance: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    max_size: int | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id or not isinstance(self.artifact_id, str):
            raise ValueError("artifact_id must be a non-empty string")
        if self.task_id is not None and not self.task_id:
            raise ValueError("task_id must be non-empty when provided")
        if any(not isinstance(item, str) or not item for item in self.provenance):
            raise ValueError("provenance must contain non-empty identifiers")
        if self.max_size is not None and self.max_size <= 0:
            raise ValueError("max_size must be positive")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class SecurityFinding:
    """A scanner hit without retaining the matched secret."""

    detector_id: str
    category: SensitiveCategory
    start: int
    end: int
    confidence: str
    rule_id: str
    crosses_boundary: bool
    action: str
    summary: str

    def __post_init__(self) -> None:
        if not self.detector_id or not self.rule_id or not self.summary:
            raise ValueError("finding identifiers and summary must be non-empty")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("finding range must be non-empty and non-negative")
        if type(self.crosses_boundary) is not bool:
            raise TypeError("crosses_boundary must be a bool")
        if not isinstance(self.summary, str):
            raise TypeError("finding summary must be a string")


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Complete or fail-closed scanner output."""

    complete_scan: bool
    findings: tuple[SecurityFinding, ...] = ()
    detector_manifest: tuple[str, ...] = ()
    error_code: str | None = None

    @classmethod
    def complete(
        cls,
        findings: tuple[SecurityFinding, ...],
        detector_manifest: tuple[str, ...] = (),
    ) -> ScanResult:
        return cls(True, tuple(findings), tuple(detector_manifest))

    @classmethod
    def indeterminate(cls, error_code: str) -> ScanResult:
        return cls(False, (), (), error_code)


@dataclass(frozen=True, slots=True)
class PreparedSanitizedArtifact:
    """A staged artifact that has no public payload/ref promotion capability."""

    attestation_id: str
    artifact_id: str
    descriptor: ArtifactDescriptor
    decision: SecurityDecision
    sanitized_payload: str
    original_digest: str
    sanitized_digest: str
    categories: tuple[SensitiveCategory, ...] = ()
    provenance: tuple[str, ...] = ()
    reason_code: str | None = None

    def to_ref(self) -> SanitizedArtifactRef:
        raise RuntimeError("prepared artifact is not committed")


@dataclass(frozen=True, slots=True)
class SanitizedArtifactRef:
    """Opaque reference to an attested, committed sanitized artifact."""

    attestation_id: str
    artifact_id: str
    task_id: str | None
    source: ArtifactSource
    kind: ArtifactKind
    purpose: ArtifactPurpose
    decision: SecurityDecision
    sanitized_digest: str
    policy_id: str
    policy_version: int
    policy_digest: str
    provenance: tuple[str, ...] = ()
