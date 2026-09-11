from dataclasses import replace

import pytest

from code_review_agent.domain.common.digests import sha256_bytes
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    ScanResult,
    SecurityDecision,
    SecurityFinding,
    SensitiveCategory,
    TrustLabel,
)
from code_review_agent.domain.security.policy import SecurityPolicy
from code_review_agent.domain.security.service import SecurityService


def make_descriptor(**overrides: object) -> ArtifactDescriptor:
    values: dict[str, object] = {
        "artifact_id": "artifact-1",
        "task_id": "task-1",
        "source": ArtifactSource.REPOSITORY,
        "trust_label": TrustLabel.UNTRUSTED_TEXT,
        "kind": ArtifactKind.DIFF,
        "purpose": ArtifactPurpose.DOMAIN_INGRESS,
    }
    values.update(overrides)
    return ArtifactDescriptor(**values)  # type: ignore[arg-type]


class FakeScanner:
    def __init__(self, result: ScanResult) -> None:
        self.result = result

    def scan(
        self, content: str, descriptor: ArtifactDescriptor, policy: SecurityPolicy
    ) -> ScanResult:
        if not self.result.detector_manifest:
            return ScanResult(
                self.result.complete_scan,
                self.result.findings,
                policy.detector_manifest,
                self.result.error_code,
            )
        return self.result


def policy(*, action: SecurityDecision = SecurityDecision.REDACTED) -> SecurityPolicy:
    return SecurityPolicy(
        policy_id="default",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset({SensitiveCategory.CREDENTIAL}),
        detector_manifest=("fake@1",),
        action_matrix={SensitiveCategory.CREDENTIAL: action},
        purpose_transitions=frozenset(
            {(ArtifactPurpose.DOMAIN_INGRESS, ArtifactPurpose.TRACE_INPUT_SNAPSHOT)}
        ),
    )


def test_safe_artifact_is_not_promoted_before_commit() -> None:
    service = SecurityService(
        FakeScanner(ScanResult.complete(())),
        policy=policy(),
    )
    prepared = service.evaluate_artifact("hello", make_descriptor())

    assert prepared.decision is SecurityDecision.SAFE
    with pytest.raises(RuntimeError):
        prepared.to_ref()
    reference = service.commit(prepared)
    assert reference.decision is SecurityDecision.SAFE
    assert (
        service.resolve(reference, expected_purpose=ArtifactPurpose.DOMAIN_INGRESS)
        == "hello"
    )


def test_empty_artifact_uses_sha256_digest_and_resolves_through_attestation() -> None:
    service = SecurityService(
        FakeScanner(ScanResult.complete(())),
        policy=policy(),
    )

    prepared = service.evaluate_artifact("", make_descriptor())
    reference = service.commit(prepared)

    assert prepared.sanitized_digest == sha256_bytes(b"")
    assert reference.sanitized_digest == sha256_bytes(b"")
    assert (
        service.resolve(reference, expected_purpose=ArtifactPurpose.DOMAIN_INGRESS)
        == ""
    )


def test_redaction_placeholder_is_stable_and_preserves_diff_structure() -> None:
    content = "@@ -1,2 +1,2 @@\n-token=abc123\n+token=abc123\n"
    finding = {
        "detector_id": "fake",
        "category": SensitiveCategory.CREDENTIAL,
        "start": content.index("abc123"),
        "end": content.index("abc123") + 6,
        "confidence": "high",
        "rule_id": "token",
        "crosses_boundary": False,
        "action": "redact",
        "summary": "credential pattern",
    }
    scan = ScanResult.complete((SecurityFinding(**finding),))  # type: ignore[arg-type]
    service = SecurityService(FakeScanner(scan), policy=policy())
    prepared = service.evaluate_artifact(content, make_descriptor())
    reference = service.commit(prepared)
    sanitized = service.resolve(
        reference, expected_purpose=ArtifactPurpose.DOMAIN_INGRESS
    )

    assert prepared.original_digest != prepared.sanitized_digest
    assert sanitized.count("\n") == content.count("\n")
    assert sanitized.startswith("@@ -1,2 +1,2 @@\n")
    assert "abc123" not in sanitized
    assert "<REDACTED:CREDENTIAL:01>" in sanitized


def test_blocked_and_indeterminate_are_fail_closed() -> None:
    blocked_service = SecurityService(
        FakeScanner(ScanResult.complete(())),
        policy=policy(action=SecurityDecision.BLOCKED),
    )
    prepared = blocked_service.evaluate_artifact("secret", make_descriptor())
    assert prepared.decision is SecurityDecision.SAFE

    finding = SecurityFinding(
        detector_id="fake",
        category=SensitiveCategory.CREDENTIAL,
        start=0,
        end=6,
        confidence="high",
        rule_id="token",
        crosses_boundary=False,
        action="block",
        summary="credential pattern",
    )
    blocked_hit = SecurityService(
        FakeScanner(ScanResult.complete((finding,))),
        policy=policy(action=SecurityDecision.BLOCKED),
    ).evaluate_artifact("secret", make_descriptor())
    assert blocked_hit.decision is SecurityDecision.BLOCKED

    indeterminate = SecurityService(
        FakeScanner(ScanResult.indeterminate("scanner_timeout")),
        policy=policy(),
    ).evaluate_artifact("secret", make_descriptor())
    assert indeterminate.decision is SecurityDecision.INDETERMINATE
    with pytest.raises(RuntimeError):
        SecurityService(
            FakeScanner(ScanResult.indeterminate("scanner_timeout")),
            policy=policy(),
        ).commit(indeterminate)


def test_scanner_crash_is_indeterminate_and_never_committable() -> None:
    class CrashingScanner:
        def scan(
            self, content: str, descriptor: ArtifactDescriptor, policy: SecurityPolicy
        ) -> ScanResult:
            raise RuntimeError("scanner response must not escape")

    service = SecurityService(CrashingScanner(), policy=policy())
    prepared = service.evaluate_artifact("secret", make_descriptor())

    assert prepared.decision is SecurityDecision.INDETERMINATE
    with pytest.raises(RuntimeError):
        service.commit(prepared)


def test_detector_manifest_mismatch_is_indeterminate() -> None:
    scan = ScanResult.complete((), detector_manifest=("different@1",))
    service = SecurityService(FakeScanner(scan), policy=policy())

    prepared = service.evaluate_artifact("safe", make_descriptor())

    assert prepared.decision is SecurityDecision.INDETERMINATE


def test_cross_purpose_resolution_and_provenance_are_rejected() -> None:
    service = SecurityService(FakeScanner(ScanResult.complete(())), policy=policy())
    reference = service.commit(service.evaluate_artifact("safe", make_descriptor()))

    with pytest.raises(ValueError):
        service.resolve(
            reference, expected_purpose=ArtifactPurpose.TRACE_INPUT_SNAPSHOT
        )
    with pytest.raises(ValueError):
        service.resolve(
            replace(reference, purpose=ArtifactPurpose.TRACE_INPUT_SNAPSHOT),
            expected_purpose=ArtifactPurpose.TRACE_INPUT_SNAPSHOT,
        )
    with pytest.raises(ValueError):
        service.resolve(
            replace(reference, provenance=("forged-attestation",)),
            expected_purpose=ArtifactPurpose.DOMAIN_INGRESS,
        )
