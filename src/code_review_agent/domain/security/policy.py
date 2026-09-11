"""Immutable security policy and non-degradation checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.security.models import (
    ArtifactPurpose,
    SecurityDecision,
    SensitiveCategory,
)

_DECISION_STRENGTH = {
    SecurityDecision.SAFE: 0,
    SecurityDecision.REDACTED: 1,
    SecurityDecision.BLOCKED: 2,
    SecurityDecision.INDETERMINATE: 3,
}


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Versioned policy used to evaluate artifacts."""

    policy_id: str
    policy_version: int
    schema_version: int
    sensitive_categories: frozenset[SensitiveCategory]
    detector_manifest: tuple[str, ...]
    action_matrix: Mapping[SensitiveCategory, SecurityDecision]
    purpose_transitions: frozenset[tuple[ArtifactPurpose, ArtifactPurpose]] = (
        frozenset()
    )
    capability_limits: frozenset[str] = frozenset()
    limits: Mapping[str, int] = field(default_factory=dict)
    policy_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.policy_id or self.policy_version <= 0 or self.schema_version <= 0:
            raise ValueError("policy identity and versions must be positive")
        normalized_actions = dict(self.action_matrix)
        normalized_limits = dict(self.limits)
        if any(value <= 0 for value in normalized_limits.values()):
            raise ValueError("policy limits must be positive")
        object.__setattr__(self, "action_matrix", MappingProxyType(normalized_actions))
        object.__setattr__(self, "limits", MappingProxyType(normalized_limits))
        digest_data = {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "schema_version": self.schema_version,
            "sensitive_categories": sorted(
                item.value for item in self.sensitive_categories
            ),
            "detector_manifest": list(self.detector_manifest),
            "action_matrix": {
                key.value: value.value
                for key, value in sorted(
                    self.action_matrix.items(), key=lambda item: item[0].value
                )
            },
            "purpose_transitions": sorted(
                [source.value, target.value]
                for source, target in self.purpose_transitions
            ),
            "capability_limits": sorted(self.capability_limits),
            "limits": dict(sorted(self.limits.items())),
        }
        object.__setattr__(self, "policy_digest", sha256_digest(digest_data))

    def is_not_weaker_than(self, baseline: SecurityPolicy) -> bool:
        """Return whether this policy preserves or strengthens baseline guarantees."""

        if self.schema_version != baseline.schema_version:
            return False
        if not baseline.sensitive_categories.issubset(self.sensitive_categories):
            return False
        if not set(baseline.detector_manifest).issubset(self.detector_manifest):
            return False
        if not baseline.purpose_transitions.issubset(self.purpose_transitions):
            return False
        if not self.capability_limits.issubset(baseline.capability_limits):
            return False
        for category, action in baseline.action_matrix.items():
            current = self.action_matrix.get(category)
            if (
                current is None
                or _DECISION_STRENGTH[current] < _DECISION_STRENGTH[action]
            ):
                return False
        for name, limit in baseline.limits.items():
            if self.limits.get(name, 0) > limit:
                return False
        return True
