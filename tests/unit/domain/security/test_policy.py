import pytest

from code_review_agent.domain.security.models import SecurityDecision, SensitiveCategory
from code_review_agent.domain.security.policy import SecurityPolicy


def test_policy_digest_is_deterministic_and_policy_is_immutable() -> None:
    policy = SecurityPolicy(
        policy_id="default",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset({SensitiveCategory.CREDENTIAL}),
        detector_manifest=("fixed-detector@1",),
        action_matrix={SensitiveCategory.CREDENTIAL: SecurityDecision.REDACTED},
    )
    same = SecurityPolicy(
        policy_id="default",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset({SensitiveCategory.CREDENTIAL}),
        detector_manifest=("fixed-detector@1",),
        action_matrix={SensitiveCategory.CREDENTIAL: SecurityDecision.REDACTED},
    )

    assert policy.policy_digest == same.policy_digest
    with pytest.raises(AttributeError):
        policy.policy_version = 2  # type: ignore[misc]


def test_policy_rejects_weaker_policy_and_allows_stricter_policy() -> None:
    baseline = SecurityPolicy(
        policy_id="default",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset({SensitiveCategory.CREDENTIAL}),
        detector_manifest=("fixed-detector@1",),
        action_matrix={SensitiveCategory.CREDENTIAL: SecurityDecision.REDACTED},
    )
    weaker = SecurityPolicy(
        policy_id="default",
        policy_version=2,
        schema_version=1,
        sensitive_categories=frozenset(),
        detector_manifest=(),
        action_matrix={},
    )
    stricter = SecurityPolicy(
        policy_id="default",
        policy_version=2,
        schema_version=1,
        sensitive_categories=frozenset(
            {SensitiveCategory.CREDENTIAL, SensitiveCategory.PRIVATE_KEY}
        ),
        detector_manifest=("fixed-detector@1", "key-detector@1"),
        action_matrix={
            SensitiveCategory.CREDENTIAL: SecurityDecision.BLOCKED,
            SensitiveCategory.PRIVATE_KEY: SecurityDecision.BLOCKED,
        },
    )

    assert not weaker.is_not_weaker_than(baseline)
    assert stricter.is_not_weaker_than(baseline)
