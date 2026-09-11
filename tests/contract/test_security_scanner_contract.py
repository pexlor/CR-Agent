from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    ScanResult,
    TrustLabel,
)
from code_review_agent.domain.security.policy import SecurityPolicy


def test_scanner_contract_returns_complete_structured_result() -> None:
    class ContractScanner:
        def scan(self, content: str, descriptor: ArtifactDescriptor, policy: SecurityPolicy) -> ScanResult:
            assert isinstance(content, str)
            assert descriptor.kind is ArtifactKind.DIFF
            assert descriptor.purpose is ArtifactPurpose.DOMAIN_INGRESS
            assert descriptor.source is ArtifactSource.REPOSITORY
            assert descriptor.trust_label is TrustLabel.UNTRUSTED_TEXT
            return ScanResult.complete(())

    descriptor = ArtifactDescriptor(
        artifact_id="artifact",
        task_id="task",
        source=ArtifactSource.REPOSITORY,
        trust_label=TrustLabel.UNTRUSTED_TEXT,
        kind=ArtifactKind.DIFF,
        purpose=ArtifactPurpose.DOMAIN_INGRESS,
    )
    result = ContractScanner().scan("diff", descriptor, object())  # type: ignore[arg-type]

    assert result.complete_scan is True
    assert result.findings == ()
