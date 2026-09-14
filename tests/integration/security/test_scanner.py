from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import code_review_agent.adapters.security.scanner as scanner_module
from code_review_agent.adapters.security.logging import (
    SecurityEvent,
    SecurityEventLogger,
)
from code_review_agent.adapters.security.scanner import (
    DetectorMatch,
    FixedSecurityScanner,
    load_packaged_security_policy,
)
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    SensitiveCategory,
    TrustLabel,
)
from code_review_agent.domain.security.service import SecurityService


def descriptor(
    *,
    source: ArtifactSource = ArtifactSource.REPOSITORY,
    kind: ArtifactKind = ArtifactKind.DIFF,
    purpose: ArtifactPurpose = ArtifactPurpose.DOMAIN_INGRESS,
) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        artifact_id="artifact-19",
        task_id="task-19",
        source=source,
        trust_label=TrustLabel.UNTRUSTED_TEXT,
        kind=kind,
        purpose=purpose,
    )


@pytest.fixture
def scanner() -> FixedSecurityScanner:
    return FixedSecurityScanner()


def categories_for(
    scanner: FixedSecurityScanner,
    content: str,
    *,
    source: ArtifactSource = ArtifactSource.REPOSITORY,
    kind: ArtifactKind = ArtifactKind.DIFF,
    purpose: ArtifactPurpose = ArtifactPurpose.DOMAIN_INGRESS,
) -> set[SensitiveCategory]:
    policy = load_packaged_security_policy()
    result = scanner.scan(
        content,
        descriptor(source=source, kind=kind, purpose=purpose),
        policy,
    )
    assert result.complete_scan is True
    assert result.detector_manifest == policy.detector_manifest
    return {finding.category for finding in result.findings}


@pytest.mark.parametrize(
    ("content", "category"),
    [
        (
            '+api_key = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"',
            SensitiveCategory.CREDENTIAL,
        ),
        (
            "-DATABASE_URL=postgresql://reviewer:MockPassword19@db.local/app",
            SensitiveCategory.CONNECTION_STRING,
        ),
        (
            " owner_email = reviewer19@example.test",
            SensitiveCategory.PERSONAL_DATA,
        ),
        (
            " phone = +86 138-0013-8019",
            SensitiveCategory.PERSONAL_DATA,
        ),
        (
            "id_card = 11010519491231002X",
            SensitiveCategory.PERSONAL_DATA,
        ),
        (
            "[BUSINESS_SENSITIVE]Project-Cedar-19[/BUSINESS_SENSITIVE]",
            SensitiveCategory.BUSINESS_SENSITIVE,
        ),
        (
            "opaque_blob = M9fK2pQ7xR4vN8zL1cH6jT3wY5uB0sDa",
            SensitiveCategory.UNKNOWN_SENSITIVE,
        ),
        (
            "-----BEGIN CERTIFICATE-----\nMIISecretCertificate19\n"
            "-----END CERTIFICATE-----",
            SensitiveCategory.CERTIFICATE_MATERIAL,
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nMIIPrivateMaterial19\n"
            "-----END PRIVATE KEY-----",
            SensitiveCategory.PRIVATE_KEY,
        ),
    ],
)
def test_fixed_rules_cover_secret_categories(
    scanner: FixedSecurityScanner,
    content: str,
    category: SensitiveCategory,
) -> None:
    assert category in categories_for(scanner, content)


@pytest.mark.parametrize(
    ("source", "kind", "purpose"),
    [
        (
            ArtifactSource.REPOSITORY,
            ArtifactKind.DIFF,
            ArtifactPurpose.DOMAIN_INGRESS,
        ),
        (
            ArtifactSource.REPOSITORY,
            ArtifactKind.REPOSITORY_TEXT,
            ArtifactPurpose.DOMAIN_INGRESS,
        ),
        (
            ArtifactSource.TOOL_OUTPUT,
            ArtifactKind.TOOL_PAYLOAD,
            ArtifactPurpose.TOOL_INGRESS,
        ),
        (
            ArtifactSource.MODEL_RESPONSE,
            ArtifactKind.MODEL_PAYLOAD,
            ArtifactPurpose.TRACE_MODEL_RESPONSE,
        ),
        (
            ArtifactSource.TRUSTED_APPLICATION,
            ArtifactKind.LOG_PAYLOAD,
            ArtifactPurpose.SECURITY_LOG,
        ),
        (
            ArtifactSource.TRUSTED_APPLICATION,
            ArtifactKind.ERROR_PAYLOAD,
            ArtifactPurpose.USER_DISPLAY,
        ),
        (
            ArtifactSource.TRUSTED_APPLICATION,
            ArtifactKind.REPORT_PAYLOAD,
            ArtifactPurpose.REPORT_DELIVERY,
        ),
    ],
)
def test_same_rules_apply_to_every_text_path(
    scanner: FixedSecurityScanner,
    source: ArtifactSource,
    kind: ArtifactKind,
    purpose: ArtifactPurpose,
) -> None:
    categories = categories_for(
        scanner,
        "password = 'UniqueMockSecretValue19'",
        source=source,
        kind=kind,
        purpose=purpose,
    )
    assert SensitiveCategory.CREDENTIAL in categories


def test_scanner_is_deterministic_and_returns_no_secret_values(
    scanner: FixedSecurityScanner,
) -> None:
    content = "token = 'UniqueMockSecretValue19'"
    policy = load_packaged_security_policy()

    first = scanner.scan(content, descriptor(), policy)
    second = scanner.scan(content, descriptor(), policy)

    assert first == second
    assert "UniqueMockSecretValue19" not in repr(first)
    assert all(
        finding.summary == "sensitive value detected" for finding in first.findings
    )


def test_diff_security_scan_preserves_structural_paths_but_redacts_hunk_secret(
    scanner: FixedSecurityScanner,
) -> None:
    path = "docs/superpowers/plans/2026-09-13-configured-monetary-budget-plan.md"
    secret = "M9fK2pQ7xR4vN8zL1cH6jT3wY5uB0sDa"
    content = (
        f"diff --git a/{path} b/{path}\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,2 @@\n"
        f"+See `{path}` for details.\n"
        f"+opaque_blob = {secret}\n"
    )
    security = SecurityService(scanner, policy=load_packaged_security_policy())

    prepared = security.evaluate_artifact(content, descriptor())

    assert f"diff --git a/{path} b/{path}" in prepared.sanitized_payload
    assert f"+++ b/{path}" in prepared.sanitized_payload
    assert "+See `<REDACTED:UNKNOWN_SENSITIVE:" in prepared.sanitized_payload
    assert secret not in prepared.sanitized_payload
    assert "<REDACTED:UNKNOWN_SENSITIVE:" in prepared.sanitized_payload


def test_diff_security_scan_does_not_treat_added_decorator_as_email(
    scanner: FixedSecurityScanner,
) -> None:
    content = (
        "diff --git a/test_a.py b/test_a.py\n"
        "--- a/test_a.py\n"
        "+++ b/test_a.py\n"
        "@@ -0,0 +1 @@\n"
        "+@respx.mock\n"
    )
    security = SecurityService(scanner, policy=load_packaged_security_policy())

    prepared = security.evaluate_artifact(content, descriptor())

    assert prepared.sanitized_payload.endswith("+@respx.mock\n")


def test_diff_security_scan_preserves_hunk_prefix_while_redacting_leading_token(
    scanner: FixedSecurityScanner,
) -> None:
    token = "task_id=1234567890abcdef1234567890abcdef1234567890abcdef"
    content = (
        "diff --git a/report.md b/report.md\n"
        "--- a/report.md\n"
        "+++ b/report.md\n"
        "@@ -0,0 +1 @@\n"
        f"+{token} result=succeeded\n"
    )
    security = SecurityService(scanner, policy=load_packaged_security_policy())

    prepared = security.evaluate_artifact(content, descriptor())

    assert prepared.sanitized_payload.splitlines()[-1].startswith(
        "+<REDACTED:UNKNOWN_SENSITIVE:"
    )
    assert token not in prepared.sanitized_payload


def test_diff_path_false_positive_does_not_allow_same_token_in_hunk(
    scanner: FixedSecurityScanner,
) -> None:
    token = "M9fK2pQ7xR4vN8zL1cH6jT3wY5uB0sDa"
    content = (
        f"diff --git a/{token} b/{token}\n"
        f"--- a/{token}\n"
        f"+++ b/{token}\n"
        "@@ -0,0 +1 @@\n"
        f"+value = {token}\n"
    )
    security = SecurityService(scanner, policy=load_packaged_security_policy())

    prepared = security.evaluate_artifact(content, descriptor())

    assert f"diff --git a/{token} b/{token}" in prepared.sanitized_payload
    assert f"+value = {token}" not in prepared.sanitized_payload
    assert "+value = <REDACTED:UNKNOWN_SENSITIVE:" in prepared.sanitized_payload


def test_scanner_rejects_policy_manifest_mismatch(
    scanner: FixedSecurityScanner,
) -> None:
    policy = load_packaged_security_policy()
    incompatible = policy.__class__(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        schema_version=policy.schema_version,
        sensitive_categories=policy.sensitive_categories,
        detector_manifest=("untrusted-detector",),
        action_matrix=policy.action_matrix,
        limits=policy.limits,
    )

    result = scanner.scan("safe text", descriptor(), incompatible)

    assert result.complete_scan is False
    assert result.error_code == "scanner_incomplete"


@pytest.mark.parametrize(
    ("engine", "expected_code"),
    [
        (lambda _content: (_ for _ in ()).throw(TimeoutError()), "scanner_timeout"),
        (lambda _content: (_ for _ in ()).throw(RuntimeError()), "scanner_unavailable"),
        (
            lambda content: (
                DetectorMatch(
                    detector_id="fixed/invalid@1",
                    category=SensitiveCategory.CREDENTIAL,
                    start=0,
                    end=len(content) + 1,
                    rule_id="invalid-range",
                ),
            ),
            "finding_range_invalid",
        ),
        (
            lambda _content: (
                DetectorMatch(
                    detector_id="fixed/conflict-a@1",
                    category=SensitiveCategory.CREDENTIAL,
                    start=0,
                    end=6,
                    rule_id="conflict-a",
                    boundary_certain=False,
                ),
            ),
            "finding_range_invalid",
        ),
    ],
)
def test_scanner_fail_closed_without_exposing_input(
    monkeypatch: pytest.MonkeyPatch,
    engine: Callable[[str], tuple[DetectorMatch, ...]],
    expected_code: str,
) -> None:
    secret = "UniqueMockSecretValue19"
    monkeypatch.setattr(scanner_module, "_fixed_engine", engine)
    scanner = FixedSecurityScanner()

    result = scanner.scan(secret, descriptor(), load_packaged_security_policy())

    assert result.complete_scan is False
    assert result.error_code == expected_code
    assert secret not in repr(result)


def test_scanner_fails_closed_for_non_utf8_text() -> None:
    result = FixedSecurityScanner().scan(
        "bad-surrogate-\udcff",
        descriptor(),
        load_packaged_security_policy(),
    )

    assert result.complete_scan is False
    assert result.error_code == "unsupported_encoding"


def test_scanner_enforces_policy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = load_packaged_security_policy()
    short_deadline = policy.__class__(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        schema_version=policy.schema_version,
        sensitive_categories=policy.sensitive_categories,
        detector_manifest=policy.detector_manifest,
        action_matrix=policy.action_matrix,
        limits={**policy.limits, "timeout_ms": 1},
    )

    def slow_engine(_content: str) -> tuple[DetectorMatch, ...]:
        time.sleep(0.05)
        return ()

    monkeypatch.setattr(scanner_module, "_fixed_engine", slow_engine)
    result = FixedSecurityScanner().scan(
        "safe text",
        descriptor(),
        short_deadline,
    )

    assert result.complete_scan is False
    assert result.error_code == "scanner_timeout"


def test_packaged_policy_has_no_repository_overrides() -> None:
    policy = load_packaged_security_policy()

    assert policy.policy_id == "code-review-agent-default"
    assert policy.detector_manifest
    assert set(policy.action_matrix) == set(SensitiveCategory)
    assert {"max_bytes", "max_findings", "timeout_ms"} <= set(policy.limits)


def test_caller_cannot_replace_fixed_detector_engine() -> None:
    with pytest.raises(TypeError):
        FixedSecurityScanner(engine=lambda _content: ())  # type: ignore[call-arg]


def test_repository_baseline_allowlist_and_network_cannot_change_scanning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = tmp_path / ".secrets.baseline"
    baseline.write_text(
        '{"plugins_used": [], "filters_used": [], "results": {}}',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("scanner attempted network access")

    monkeypatch.setattr("socket.socket.connect", reject_network)
    secret = "UniqueMockSecretValue19"
    categories = categories_for(
        FixedSecurityScanner(),
        f"password = '{secret}'  # pragma: allowlist secret",
    )

    assert SensitiveCategory.CREDENTIAL in categories


def test_redacted_secret_never_reaches_sqlite_report_log_or_error(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "UniqueMockSecretValue19"
    policy = load_packaged_security_policy()
    service = SecurityService(FixedSecurityScanner(), policy=policy)
    prepared = service.evaluate_artifact(
        f"password = '{secret}'",
        descriptor(
            kind=ArtifactKind.REPORT_PAYLOAD,
            purpose=ArtifactPurpose.REPORT_DELIVERY,
        ),
    )
    reference = service.commit(prepared)
    sanitized = service.resolve(
        reference,
        expected_purpose=ArtifactPurpose.REPORT_DELIVERY,
    )

    database_path = tmp_path / "security.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE artifacts (payload TEXT NOT NULL)")
        connection.execute("INSERT INTO artifacts VALUES (?)", (sanitized,))
    report_path = tmp_path / "report.md"
    report_path.write_text(sanitized, encoding="utf-8")

    logger = SecurityEventLogger(logging.getLogger("security-leak-test"))
    with caplog.at_level(logging.INFO, logger="security-leak-test"):
        logger.emit(
            SecurityEvent(
                event_id="event-19",
                event_type="artifact_redacted",
                reason_code="sensitive_content_redacted",
                task_id="task-19",
                decision="redacted",
                categories=("credential",),
                finding_count=1,
            )
        )

    monkeypatch.setattr(
        scanner_module,
        "_fixed_engine",
        lambda _content: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    crash_result = FixedSecurityScanner().scan(secret, descriptor(), policy)

    assert secret not in database_path.read_bytes().decode("latin-1")
    assert secret not in report_path.read_text(encoding="utf-8")
    assert all(secret not in message for message in caplog.messages)
    assert secret not in repr(crash_result)
    assert "<REDACTED:CREDENTIAL:" in sanitized


def test_incomplete_policy_fails_closed() -> None:
    policy = load_packaged_security_policy()
    actions = dict(policy.action_matrix)
    del actions[SensitiveCategory.PERSONAL_DATA]
    incomplete = policy.__class__(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        schema_version=policy.schema_version,
        sensitive_categories=policy.sensitive_categories,
        detector_manifest=policy.detector_manifest,
        action_matrix=actions,
        limits=policy.limits,
    )

    result = FixedSecurityScanner().scan(
        "owner = reviewer19@example.test",
        descriptor(),
        incomplete,
    )

    assert result.complete_scan is False
    assert result.error_code == "security_policy_invalid"
