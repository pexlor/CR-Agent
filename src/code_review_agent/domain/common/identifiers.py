"""Strongly typed UUIDv4 identifiers used by the domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self
from uuid import RFC_4122, UUID, uuid4


@dataclass(frozen=True, slots=True)
class Identifier:
    """A canonical, lowercase RFC 4122 UUIDv4 value."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("identifier must be a string")
        try:
            parsed = UUID(self.value)
        except (AttributeError, ValueError) as exc:
            raise ValueError("identifier must be a UUIDv4") from exc
        if (
            str(parsed) != self.value
            or parsed.version != 4
            or parsed.variant != RFC_4122
        ):
            raise ValueError("identifier must be a canonical lowercase UUIDv4")

    @classmethod
    def new(cls) -> Self:
        return cls(str(uuid4()))

    @classmethod
    def parse(cls, value: str) -> Self:
        return cls(value)

    def __str__(self) -> str:
        return self.value


class TaskId(Identifier):
    """Task aggregate identifier."""


class SpecId(Identifier):
    """Task specification identifier."""


class InputBindingId(Identifier):
    """Normalized input binding identifier."""


class CheckpointId(Identifier):
    """Recovery checkpoint identifier."""


class ResultSnapshotId(Identifier):
    """Immutable result snapshot identifier."""


class ExecutionId(Identifier):
    """Execution attempt identifier."""


class WorkUnitId(Identifier):
    """Review work unit identifier."""


class TraceEventId(Identifier):
    """Trace event identifier."""


class FindingId(Identifier):
    """Final finding identifier."""
