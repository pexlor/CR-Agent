from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from code_review_agent.adapters.input.plain_diff import PlainDiffProvider
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.input.models import InputLimits
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    ScanResult,
    SecurityDecision,
    SensitiveCategory,
    TrustLabel,
)
from code_review_agent.domain.security.policy import SecurityPolicy
from code_review_agent.domain.security.service import SecurityService
from code_review_agent.ports.input import InputProviderPort, SecurityBoundaryPort


class CompleteScanner:
    def scan(
        self,
        content: str,
        descriptor: ArtifactDescriptor,
        policy: SecurityPolicy,
    ) -> ScanResult:
        assert descriptor.source is ArtifactSource.USER_CLI
        assert descriptor.trust_label is TrustLabel.UNTRUSTED_TEXT
        assert descriptor.kind is ArtifactKind.DIFF
        assert descriptor.purpose is ArtifactPurpose.DOMAIN_INGRESS
        return ScanResult.complete((), policy.detector_manifest)


def provider() -> PlainDiffProvider:
    policy = SecurityPolicy(
        policy_id="input-contract",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset({SensitiveCategory.CREDENTIAL}),
        detector_manifest=("complete@1",),
        action_matrix={SensitiveCategory.CREDENTIAL: SecurityDecision.REDACTED},
        purpose_transitions=frozenset(),
    )
    return PlainDiffProvider(SecurityService(CompleteScanner(), policy=policy))


def test_provider_satisfies_port_and_file_path_is_not_part_of_identity(
    tmp_path: Path,
) -> None:
    content = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    path = tmp_path / "arbitrary-name.patch"
    path.write_text(content, encoding="utf-8")
    value = provider()

    assert isinstance(value, InputProviderPort)
    from_text = value.acquire_text(task_id="task-1", content=content)
    from_file = value.acquire_file(task_id="task-1", path=path)

    assert from_text.identity == from_file.identity
    assert from_text.identity.content_digest == from_file.identity.content_digest
    assert str(path) not in repr(from_file)


def test_provider_accepts_exactly_one_mib_and_rejects_one_byte_more() -> None:
    value = provider()
    exact = "x" * InputLimits().max_bytes

    acquired = value.acquire_text(task_id="task-1", content=exact)
    assert acquired.byte_count == InputLimits().max_bytes

    with pytest.raises(StableError) as captured:
        value.acquire_text(task_id="task-1", content=exact + "x")
    assert captured.value.code == "input_too_large"
    assert captured.value.details == {
        "limit": 1_048_576,
        "metric": "bytes",
    }


def test_provider_rejects_malformed_utf8_without_retaining_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.diff"
    path.write_bytes(b"diff --git a/a b/a\n\xffprivate-marker")

    with pytest.raises(StableError) as captured:
        provider().acquire_file(task_id="task-1", path=path)

    assert captured.value.code == "input_invalid_utf8"
    assert captured.value.__cause__ is None
    assert "private-marker" not in repr(captured.value)
    assert str(path) not in str(captured.value.to_dict())


def test_provider_maps_unreadable_file_to_stable_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.diff"

    with pytest.raises(StableError) as captured:
        provider().acquire_file(task_id="task-1", path=missing)

    assert captured.value.code == "input_unreadable"
    assert captured.value.__cause__ is None
    assert str(missing) not in str(captured.value.to_dict())


def test_provider_hides_security_boundary_exception_payload() -> None:
    marker = "SIMULATED_UNTRUSTED_PAYLOAD"

    class CrashingBoundary:
        def evaluate_artifact(
            self, content: str, descriptor: ArtifactDescriptor
        ) -> object:
            raise RuntimeError(content)

    value = PlainDiffProvider(cast(SecurityBoundaryPort, CrashingBoundary()))

    with pytest.raises(StableError) as captured:
        value.acquire_text(task_id="task-1", content=marker)

    assert captured.value.code == "security_boundary_failed"
    assert captured.value.__cause__ is None
    assert marker not in repr(captured.value)
    assert marker not in str(captured.value.to_dict())
