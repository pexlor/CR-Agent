"""Fixed, offline sensitive-data scanner adapter."""

from __future__ import annotations

import math
import re
import signal
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from importlib.resources import files
from types import FrameType
from typing import Final

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import transient_settings

from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactPurpose,
    ScanResult,
    SecurityDecision,
    SecurityFinding,
    SensitiveCategory,
)
from code_review_agent.domain.security.policy import SecurityPolicy

_DETECT_SECRETS_VERSION: Final = version("detect-secrets")
_PLUGIN_CATEGORIES: Final[Mapping[str, SensitiveCategory]] = {
    "AWS Access Key": SensitiveCategory.CLOUD_SECRET,
    "Basic Auth Credentials": SensitiveCategory.CREDENTIAL,
    "GitHub Token": SensitiveCategory.CREDENTIAL,
    "GitLab Token": SensitiveCategory.CREDENTIAL,
    "JSON Web Token": SensitiveCategory.CREDENTIAL,
    "OpenAI Token": SensitiveCategory.CREDENTIAL,
    "Private Key": SensitiveCategory.PRIVATE_KEY,
    "Secret Keyword": SensitiveCategory.CREDENTIAL,
    "Slack Token": SensitiveCategory.CREDENTIAL,
}
_PLUGIN_NAMES: Final = (
    "AWSKeyDetector",
    "BasicAuthDetector",
    "GitHubTokenDetector",
    "GitLabTokenDetector",
    "JwtTokenDetector",
    "OpenAIDetector",
    "PrivateKeyDetector",
    "KeywordDetector",
    "SlackDetector",
)
_PLUGIN_IDS: Final[Mapping[str, str]] = {
    secret_type: f"detect-secrets/{plugin_name}@{_DETECT_SECRETS_VERSION}"
    for secret_type, plugin_name in zip(_PLUGIN_CATEGORIES, _PLUGIN_NAMES, strict=True)
}
_CUSTOM_DETECTOR_IDS: Final = (
    "fixed/connection-string@1",
    "fixed/pem-block@1",
    "fixed/pii@1",
    "fixed/business-sensitive@1",
    "fixed/unknown-sensitive@1",
)
FIXED_DETECTOR_MANIFEST: Final = (*_PLUGIN_IDS.values(), *_CUSTOM_DETECTOR_IDS)
_SCAN_LOCK: Final = threading.RLock()

_CONNECTION_STRING = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)://"
    r"[^\s/@:]+:[^\s/@]+@[^\s]+",
    re.IGNORECASE,
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
_CERTIFICATE_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d[ -]?\d{4}[ -]?\d{4}(?!\d)")
_CN_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\w)")
_BUSINESS_BLOCK = re.compile(
    r"\[BUSINESS_SENSITIVE\].*?\[/BUSINESS_SENSITIVE\]",
    re.DOTALL | re.IGNORECASE,
)
_OPAQUE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])"
)


@dataclass(frozen=True, slots=True)
class DetectorMatch:
    """Internal detector result that never stores the matched value."""

    detector_id: str
    category: SensitiveCategory
    start: int
    end: int
    rule_id: str
    confidence: str = "high"
    boundary_certain: bool = True


class FixedSecurityScanner:
    """Run only the application-owned detector set against in-memory text."""

    def scan(
        self,
        content: str,
        descriptor: ArtifactDescriptor,
        policy: SecurityPolicy,
    ) -> ScanResult:
        del descriptor
        if tuple(policy.detector_manifest) != FIXED_DETECTOR_MANIFEST:
            return ScanResult.indeterminate("scanner_incomplete")
        if not _policy_is_complete(policy):
            return ScanResult.indeterminate("security_policy_invalid")
        try:
            encoded = content.encode("utf-8", errors="strict")
        except (UnicodeEncodeError, AttributeError):
            return ScanResult.indeterminate("unsupported_encoding")
        if len(encoded) > policy.limits.get("max_bytes", 0):
            return ScanResult.indeterminate("artifact_too_large")

        timeout_ms = policy.limits.get("timeout_ms", 0)
        try:
            started = time.monotonic()
            with _time_limit(timeout_ms), _SCAN_LOCK:
                matches = tuple(_fixed_engine(content))
            if (time.monotonic() - started) * 1000 > timeout_ms:
                raise TimeoutError
        except TimeoutError:
            return ScanResult.indeterminate("scanner_timeout")
        except Exception:
            return ScanResult.indeterminate("scanner_unavailable")

        if len(matches) > policy.limits.get("max_findings", 0):
            return ScanResult.indeterminate("scanner_incomplete")
        if not _ranges_are_valid(matches, len(content)):
            return ScanResult.indeterminate("finding_range_invalid")

        findings = tuple(
            SecurityFinding(
                detector_id=match.detector_id,
                category=match.category,
                start=match.start,
                end=match.end,
                confidence=match.confidence,
                rule_id=match.rule_id,
                crosses_boundary=False,
                action=policy.action_matrix[match.category].value,
                summary="sensitive value detected",
            )
            for match in sorted(
                set(matches),
                key=lambda item: (
                    item.start,
                    item.end,
                    item.category.value,
                    item.detector_id,
                    item.rule_id,
                ),
            )
        )
        return ScanResult.complete(findings, FIXED_DETECTOR_MANIFEST)


def load_packaged_security_policy() -> SecurityPolicy:
    """Load the immutable policy distributed with the application."""

    policy_path = files("code_review_agent.resources").joinpath("security_policy.toml")
    data = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    actions = {
        SensitiveCategory(name): SecurityDecision(value)
        for name, value in data["actions"].items()
    }
    transitions = frozenset(
        (ArtifactPurpose(item["source"]), ArtifactPurpose(item["target"]))
        for item in data.get("purpose_transitions", ())
    )
    return SecurityPolicy(
        policy_id=data["policy"]["id"],
        policy_version=data["policy"]["version"],
        schema_version=data["policy"]["schema_version"],
        sensitive_categories=frozenset(actions),
        detector_manifest=tuple(data["detectors"]["manifest"]),
        action_matrix=actions,
        purpose_transitions=transitions,
        limits=data["limits"],
    )


def _fixed_engine(content: str) -> tuple[DetectorMatch, ...]:
    matches = [*_detect_secrets_matches(content), *_custom_matches(content)]
    return tuple(matches)


def _detect_secrets_matches(content: str) -> Iterator[DetectorMatch]:
    config = {
        "plugins_used": [{"name": name} for name in _PLUGIN_NAMES],
        "filters_used": [],
    }
    offset = 0
    with transient_settings(config):
        for line in content.splitlines(keepends=True):
            searchable = line.rstrip("\r\n")
            for secret in scan_line(searchable):
                category = _PLUGIN_CATEGORIES.get(secret.type)
                detector_id = _PLUGIN_IDS.get(secret.type)
                secret_value = secret.secret_value
                if (
                    category is None
                    or detector_id is None
                    or not isinstance(secret_value, str)
                    or not secret_value
                ):
                    raise RuntimeError("unknown fixed detector result")
                start_at = 0
                while True:
                    start = searchable.find(secret_value, start_at)
                    if start < 0:
                        break
                    yield DetectorMatch(
                        detector_id=detector_id,
                        category=category,
                        start=offset + start,
                        end=offset + start + len(secret_value),
                        rule_id=secret.type.lower().replace(" ", "-"),
                    )
                    start_at = start + len(secret_value)
            offset += len(line)
    if content and not content.splitlines(keepends=True):
        raise RuntimeError("text segmentation failed")


def _custom_matches(content: str) -> Iterator[DetectorMatch]:
    yield from _regex_matches(
        content,
        _CONNECTION_STRING,
        "fixed/connection-string@1",
        SensitiveCategory.CONNECTION_STRING,
        "credentialed-uri",
    )
    yield from _regex_matches(
        content,
        _PRIVATE_KEY_BLOCK,
        "fixed/pem-block@1",
        SensitiveCategory.PRIVATE_KEY,
        "private-key-block",
    )
    yield from _regex_matches(
        content,
        _CERTIFICATE_BLOCK,
        "fixed/pem-block@1",
        SensitiveCategory.CERTIFICATE_MATERIAL,
        "certificate-block",
    )
    for pattern, rule_id in ((_EMAIL, "email"), (_PHONE, "phone"), (_CN_ID, "cn-id")):
        yield from _regex_matches(
            content,
            pattern,
            "fixed/pii@1",
            SensitiveCategory.PERSONAL_DATA,
            rule_id,
        )
    yield from _regex_matches(
        content,
        _BUSINESS_BLOCK,
        "fixed/business-sensitive@1",
        SensitiveCategory.BUSINESS_SENSITIVE,
        "explicit-business-sensitive-block",
    )
    for match in _OPAQUE_TOKEN.finditer(content):
        value = match.group(0)
        if _looks_high_entropy(value):
            yield DetectorMatch(
                detector_id="fixed/unknown-sensitive@1",
                category=SensitiveCategory.UNKNOWN_SENSITIVE,
                start=match.start(),
                end=match.end(),
                rule_id="high-entropy-opaque-token",
                confidence="medium",
            )


def _regex_matches(
    content: str,
    pattern: re.Pattern[str],
    detector_id: str,
    category: SensitiveCategory,
    rule_id: str,
) -> Iterator[DetectorMatch]:
    for match in pattern.finditer(content):
        yield DetectorMatch(
            detector_id=detector_id,
            category=category,
            start=match.start(),
            end=match.end(),
            rule_id=rule_id,
        )


def _looks_high_entropy(value: str) -> bool:
    counts = Counter(value)
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in counts.values()
    )
    return (
        entropy >= 4.0
        and any(char.isalpha() for char in value)
        and any(char.isdigit() for char in value)
    )


def _ranges_are_valid(matches: tuple[DetectorMatch, ...], length: int) -> bool:
    return all(
        match.detector_id in FIXED_DETECTOR_MANIFEST
        and match.rule_id
        and match.boundary_certain
        and 0 <= match.start < match.end <= length
        for match in matches
    )


def _policy_is_complete(policy: SecurityPolicy) -> bool:
    categories = frozenset(SensitiveCategory)
    return (
        policy.sensitive_categories == categories
        and frozenset(policy.action_matrix) == categories
        and all(
            policy.limits.get(name, 0) > 0
            for name in ("max_bytes", "max_findings", "timeout_ms")
        )
    )


@contextmanager
def _time_limit(timeout_ms: int) -> Iterator[None]:
    if timeout_ms <= 0:
        raise TimeoutError
    can_interrupt = (
        threading.current_thread() is threading.main_thread()
        and hasattr(signal, "setitimer")
        and hasattr(signal, "SIGALRM")
    )
    if not can_interrupt:
        yield
        return

    def raise_timeout(_signum: int, _frame: FrameType | None) -> None:
        raise TimeoutError

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_ms / 1000)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)
