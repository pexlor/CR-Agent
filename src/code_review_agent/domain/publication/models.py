"""Immutable contracts for remote review publication."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PublicationItemKind(StrEnum):
    LINE = "line"
    SUMMARY = "summary"


class PublicationItemState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PublicationState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    platform: str
    source_url: str
    repository: str
    number: int
    base_sha: str
    start_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class PublicationPosition:
    path: str
    old_path: str
    new_path: str
    line: int
    side: str


@dataclass(frozen=True, slots=True)
class PublicationItem:
    publication_key: str
    kind: PublicationItemKind
    body: str
    body_digest: str
    marker: str
    finding_id: str | None = None
    position: PublicationPosition | None = None

    def __post_init__(self) -> None:
        if self.kind is PublicationItemKind.LINE:
            if self.finding_id is None or self.position is None:
                raise ValueError("line_publication_requires_position")
        elif self.position is not None or self.finding_id is not None:
            raise ValueError("summary_publication_cannot_have_position")


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    publication_id: str
    task_id: str
    snapshot_id: str
    snapshot_version: int
    target: PublicationTarget
    items: tuple[PublicationItem, ...]

    def __post_init__(self) -> None:
        if not self.items or self.items[-1].kind is not PublicationItemKind.SUMMARY:
            raise ValueError("publication_summary_required")
        keys = tuple(item.publication_key for item in self.items)
        if len(keys) != len(set(keys)):
            raise ValueError("publication_keys_must_be_unique")


@dataclass(frozen=True, slots=True)
class RemoteTargetState:
    open: bool
    head_sha: str


@dataclass(frozen=True, slots=True)
class RemoteComment:
    remote_id: str
    url: str


@dataclass(frozen=True, slots=True)
class PublicationItemResult:
    publication_key: str
    kind: str
    state: str
    remote_id: str | None = None
    remote_url: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationResult:
    task_id: str
    publication_id: str
    state: str
    published: int
    skipped: int
    failed: int
    unknown: int
    items: tuple[PublicationItemResult, ...]


class PublicationError(RuntimeError):
    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


def aggregate_state(items: tuple[PublicationItemResult, ...]) -> PublicationState:
    states = {item.state for item in items}
    if PublicationItemState.UNKNOWN.value in states:
        return PublicationState.UNKNOWN
    if states == {PublicationItemState.SUCCEEDED.value}:
        return PublicationState.SUCCEEDED
    succeeded = PublicationItemState.SUCCEEDED.value in states
    failed = PublicationItemState.FAILED.value in states
    if succeeded and failed:
        return PublicationState.PARTIAL
    if failed and not succeeded:
        return PublicationState.FAILED
    return PublicationState.PENDING
