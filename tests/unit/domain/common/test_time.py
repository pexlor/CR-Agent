from datetime import datetime, timedelta, timezone

import pytest

from code_review_agent.domain.common.time import FixedClock, SystemClock, ensure_utc


def test_ensure_utc_accepts_aware_utc_and_rejects_naive_or_non_utc() -> None:
    value = datetime(2026, 9, 11, 1, 2, 3, tzinfo=timezone.utc)

    assert ensure_utc(value) is value
    with pytest.raises(ValueError):
        ensure_utc(value.replace(tzinfo=None))
    with pytest.raises(ValueError):
        ensure_utc(value.astimezone(timezone(timedelta(hours=8))))


def test_clocks_return_utc_values() -> None:
    fixed = FixedClock(datetime(2026, 9, 11, tzinfo=timezone.utc))

    assert fixed.now().tzinfo == timezone.utc
    assert SystemClock().now().tzinfo == timezone.utc
