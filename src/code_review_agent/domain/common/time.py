"""Injectable UTC clock contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


def ensure_utc(value: datetime) -> datetime:
    """Validate a timezone-aware UTC datetime and normalize its tzinfo."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("datetime must use UTC")
    if value.tzinfo is UTC:
        return value
    return value.astimezone(UTC)


class Clock(Protocol):
    """Source of current UTC time for domain decisions."""

    def now(self) -> datetime:
        """Return the current UTC time."""


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Production clock backed by the system UTC clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    """Deterministic clock for tests and replay."""

    current: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "current", ensure_utc(self.current))

    def now(self) -> datetime:
        return self.current
