from __future__ import annotations

from code_review_agent.adapters.security.scanner import (
    FixedSecurityScanner,
    load_packaged_security_policy,
)
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    TrustLabel,
)


def test_security_matrix_redacts_simulated_credentials() -> None:
    descriptor = ArtifactDescriptor(
        artifact_id="acceptance-security",
        task_id="acceptance",
        source=ArtifactSource.REPOSITORY,
        trust_label=TrustLabel.UNTRUSTED_TEXT,
        kind=ArtifactKind.DIFF,
        purpose=ArtifactPurpose.DOMAIN_INGRESS,
    )
    content = "password = 'AcceptanceOnlyMockSecret19'"
    result = FixedSecurityScanner().scan(
        content, descriptor, load_packaged_security_policy()
    )

    assert result.complete_scan is True
    assert "AcceptanceOnlyMockSecret19" not in repr(result)
