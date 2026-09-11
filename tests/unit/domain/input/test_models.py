from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from code_review_agent.domain.input.models import (
    ChangeType,
    CompletenessStatus,
    CoverageStatus,
    InputIdentity,
    InputLimits,
)


def test_plain_diff_identity_is_stable_and_immutable() -> None:
    first = InputIdentity.for_plain_diff("a" * 64)
    second = InputIdentity.for_plain_diff("a" * 64)

    assert first == second
    assert first.identity_digest == second.identity_digest
    assert first.display_name == f"plain diff {first.identity_digest[:12]}"
    with pytest.raises(FrozenInstanceError):
        first.content_digest = "b" * 64  # type: ignore[misc]


def test_input_limits_are_the_frozen_product_limits() -> None:
    limits = InputLimits()

    assert limits.max_bytes == 1_048_576
    assert limits.max_files == 200
    assert limits.max_changed_lines == 10_000
    assert limits.max_physical_lines == 20_000
    with pytest.raises(ValueError):
        InputLimits(max_files=0)


def test_input_enums_make_completeness_and_coverage_explicit() -> None:
    assert CompletenessStatus.COMPLETE.value == "complete"
    assert CoverageStatus.PARTIAL.value == "partial"
    assert ChangeType.RENAMED.value == "renamed"
    assert datetime(2026, 9, 10, tzinfo=UTC).tzinfo is UTC
