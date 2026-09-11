"""Immutable token budget value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

MAX_AUTHORIZATION = 1_000_000


class TokenAmount(int):
    """A non-negative integer token amount."""

    def __new__(cls, value: int) -> TokenAmount:
        if type(value) is not int or value < 0:
            raise ValueError("token amount must be a non-negative integer")
        return int.__new__(cls, value)

    @classmethod
    def authorization(cls, value: int) -> TokenAmount:
        if type(value) is not int or not 1 <= value <= MAX_AUTHORIZATION:
            raise ValueError("authorization must be between 1 and 1,000,000 tokens")
        return cls(value)


class BudgetAccountState(StrEnum):
    OPEN = "open"
    FROZEN_OVERAGE = "frozen_overage"
    CLOSED = "closed"


class ReservationState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    SETTLED_KNOWN = "settled_known"
    SETTLED_UNCERTAIN = "settled_uncertain"


class UsageState(StrEnum):
    KNOWN = "known"
    MISSING = "missing"
    UNTRUSTED = "untrusted"


class LedgerEntryType(StrEnum):
    AUTHORIZATION_INITIAL = "authorization_initial"
    AUTHORIZATION_ADDED = "authorization_added"
    RESERVATION_CREATED = "reservation_created"
    RESERVATION_RELEASED = "reservation_released"
    USAGE_SETTLED = "usage_settled"
    UNKNOWN_COMMITTED = "unknown_committed"
    USAGE_OVERAGE_RECORDED = "usage_overage_recorded"
    FREEZE_APPLIED = "freeze_applied"
    FREEZE_CLEARED = "freeze_cleared"
    ACCOUNT_CLOSED = "account_closed"


@dataclass(frozen=True, slots=True)
class BudgetAccount:
    account_id: str
    task_id: str
    unit: str
    state: BudgetAccountState
    revision: int
    last_sequence: int
    provider_capability_baseline_ref: str
    active_freeze_id: str | None = None
    pending_revalidation: bool = False

    def __post_init__(self) -> None:
        if not self.account_id or not self.task_id or self.unit != "token":
            raise ValueError("budget account identity and token unit are required")
        if self.revision < 1 or self.last_sequence < 0:
            raise ValueError("budget account versions must be non-negative/positive")


@dataclass(frozen=True, slots=True)
class NormalizedActualUsage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if type(self.input_tokens) is not int or type(self.output_tokens) is not int:
            raise ValueError("usage fields must be integers")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("usage fields must be non-negative")

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ProviderHardBudgetCapability:
    capability_id: str
    provider_id: str
    provider_version: str
    model_id: str
    origin: str
    prepared_request_digest: str
    token_counted_request_digest: str
    max_output_enforced: bool
    usage_mapping_trusted: bool
    retries_disabled: bool
    valid: bool = True
    verified_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.capability_id,
                self.provider_id,
                self.provider_version,
                self.model_id,
                self.origin,
                self.prepared_request_digest,
                self.token_counted_request_digest,
            )
        ):
            raise ValueError("provider capability identity is required")
        if self.prepared_request_digest != self.token_counted_request_digest:
            raise ValueError("prepared and counted request digests must match")
        if (
            self.verified_at.tzinfo is None
            or self.verified_at.utcoffset() != UTC.utcoffset(self.verified_at)
        ):
            raise ValueError("verified_at must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    account_id: str
    task_id: str
    model_call_id: str
    work_unit_id: str
    input_bound: int
    output_max: int
    amount: int
    prepared_request_digest: str
    capability_ref: str
    state: ReservationState
    usage_state: UsageState = UsageState.KNOWN
    actual_usage: int | None = None

    def __post_init__(self) -> None:
        if not self.reservation_id or not self.account_id or not self.task_id:
            raise ValueError("reservation identity is required")
        if (
            self.input_bound < 0
            or self.output_max < 0
            or self.amount != self.input_bound + self.output_max
        ):
            raise ValueError(
                "reservation amount must equal input bound plus output max"
            )
        if self.amount <= 0:
            raise ValueError("reservation amount must be positive")
        if self.actual_usage is not None and self.actual_usage < 0:
            raise ValueError("actual usage must be non-negative")


@dataclass(frozen=True, slots=True)
class ProviderBudgetCapability:
    """Alias kept for callers that use the shorter capability name."""

    capability: ProviderHardBudgetCapability


@dataclass(frozen=True, slots=True)
class ReservationDenied:
    code: str
    account_id: str
    requested: int
    available: int


@dataclass(frozen=True, slots=True)
class BudgetSettlement:
    reservation: BudgetReservation
    actual_usage: int
    overage: int = 0


@dataclass(frozen=True, slots=True)
class BudgetLedgerEntry:
    entry_id: str
    entry_type: LedgerEntryType
    amount: int
    sequence: int
    reservation_amount: int = 0
    usage_state: UsageState | None = None
    created_at: datetime = datetime.now(UTC)

    def __post_init__(self) -> None:
        if (
            not self.entry_id
            or self.sequence <= 0
            or self.amount < 0
            or self.reservation_amount < 0
        ):
            raise ValueError("ledger entry fields must be non-negative and identified")
        if (
            self.created_at.tzinfo is None
            or self.created_at.utcoffset() != UTC.utcoffset(self.created_at)
        ):
            raise ValueError("created_at must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class BudgetSummary:
    task_id: str
    ledger_version: int
    authorized: int
    known_consumption: int
    uncertain_consumption: int
    active_reservations: int
    released_reservations: int
    overage: int
    remaining_budget: int
    authorization_deficit: int
    spendable_budget: int
    account_state: BudgetAccountState
    pending_revalidation: bool = False

    @property
    def committed(self) -> int:
        return (
            self.known_consumption
            + self.uncertain_consumption
            + self.active_reservations
        )
