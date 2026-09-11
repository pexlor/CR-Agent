from datetime import UTC, datetime

import pytest

from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    SecurityDecision,
    SecurityFinding,
    SensitiveCategory,
    TrustLabel,
)


def descriptor(**overrides: object) -> ArtifactDescriptor:
    values: dict[str, object] = {
        "artifact_id": "artifact-1",
        "task_id": "task-1",
        "source": ArtifactSource.USER_CLI,
        "trust_label": TrustLabel.UNTRUSTED_TEXT,
        "kind": ArtifactKind.DIFF,
        "purpose": ArtifactPurpose.DOMAIN_INGRESS,
    }
    values.update(overrides)
    return ArtifactDescriptor(**values)


def test_descriptor_and_finding_are_immutable_and_do_not_hold_secret_values() -> None:
    item = SecurityFinding(
        detector_id="fixed-detector",
        category=SensitiveCategory.CREDENTIAL,
        start=2,
        end=8,
        confidence="high",
        rule_id="api-token",
        crosses_boundary=False,
        action="redact",
        summary="credential pattern detected",
    )
    artifact = descriptor()

    assert item.summary == "credential pattern detected"
    assert artifact.purpose is ArtifactPurpose.DOMAIN_INGRESS
    with pytest.raises(AttributeError):
        artifact.kind = ArtifactKind.PROMPT


def test_finding_rejects_invalid_ranges_and_raw_value_fields() -> None:
    with pytest.raises(ValueError):
        SecurityFinding(
            detector_id="detector",
            category=SensitiveCategory.UNKNOWN_SENSITIVE,
            start=4,
            end=4,
            confidence="medium",
            rule_id="unknown",
            crosses_boundary=False,
            action="redact",
            summary="unknown sensitive pattern",
        )
    with pytest.raises(TypeError):
        SecurityFinding(
            detector_id="detector",
            category=SensitiveCategory.CREDENTIAL,
            start=0,
            end=2,
            confidence="high",
            rule_id="token",
            crosses_boundary=False,
            action="redact",
            summary=object(),  # type: ignore[arg-type]
        )


def test_decision_and_descriptor_enums_are_explicit() -> None:
    assert {item.value for item in SecurityDecision} == {
        "safe",
        "redacted",
        "blocked",
        "indeterminate",
    }
    assert descriptor(created_at=datetime.now(UTC)).created_at.tzinfo is UTC
