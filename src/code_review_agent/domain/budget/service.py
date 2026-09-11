"""Token budget state transitions for the local domain runtime."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from code_review_agent.domain.budget.ledger import BudgetLedger
from code_review_agent.domain.budget.models import (
    MAX_AUTHORIZATION,
    BudgetAccount,
    BudgetAccountState,
    BudgetLedgerEntry,
    BudgetReservation,
    BudgetSettlement,
    BudgetSummary,
    LedgerEntryType,
    NormalizedActualUsage,
    ProviderHardBudgetCapability,
    ReservationDenied,
    ReservationState,
    UsageState,
)


class BudgetService:
    """In-memory domain implementation used before the SQLite adapter exists."""

    def __init__(self) -> None:
        self._accounts: dict[str, BudgetAccount] = {}
        self._entries: dict[str, list[BudgetLedgerEntry]] = {}
        self._reservations: dict[str, BudgetReservation] = {}
        self._calls: dict[str, str] = {}

    def create_account(
        self, task_id: str, requested_tokens: int, *, capability_ref: str
    ) -> BudgetAccount:
        if (
            type(requested_tokens) is not int
            or not 1 <= requested_tokens <= MAX_AUTHORIZATION
        ):
            raise ValueError("invalid_budget")
        account = BudgetAccount(
            account_id=str(uuid4()),
            task_id=task_id,
            unit="token",
            state=BudgetAccountState.OPEN,
            revision=1,
            last_sequence=1,
            provider_capability_baseline_ref=capability_ref,
        )
        self._accounts[account.account_id] = account
        self._entries[account.account_id] = [
            BudgetLedger.entry(
                LedgerEntryType.AUTHORIZATION_INITIAL, requested_tokens, 1
            )
        ]
        return account

    def validate_initial_authorization(
        self, task_id: str, requested_tokens: int, *, capability_ref: str
    ) -> BudgetAccount:
        """Validate and create the task's initial token authorization."""

        return self.create_account(
            task_id, requested_tokens, capability_ref=capability_ref
        )

    def prepare_reservation(
        self,
        account_id: str,
        *,
        model_call_id: str,
        work_unit_id: str,
        input_bound: int,
        output_max: int,
        prepared_request_digest: str,
        capability: ProviderHardBudgetCapability,
    ) -> BudgetReservation | ReservationDenied:
        account = self._account(account_id)
        if account.state is not BudgetAccountState.OPEN or account.pending_revalidation:
            return ReservationDenied(
                "budget_frozen", account_id, input_bound + output_max, 0
            )
        if model_call_id in self._calls:
            raise ValueError("reservation already exists for model call")
        if (
            not capability.valid
            or capability.capability_id != account.provider_capability_baseline_ref
        ):
            raise ValueError("provider_hard_budget_incompatible")
        if (
            capability.prepared_request_digest != prepared_request_digest
            or capability.token_counted_request_digest != prepared_request_digest
        ):
            raise ValueError("prepared_request_mismatch")
        if input_bound < 0 or output_max < 0 or input_bound + output_max <= 0:
            raise ValueError("invalid_budget")
        amount = input_bound + output_max
        available = self.get_summary(account_id).spendable_budget
        if amount > available:
            return ReservationDenied(
                "insufficient_budget", account_id, amount, available
            )
        reservation = BudgetReservation(
            reservation_id=str(uuid4()),
            account_id=account_id,
            task_id=account.task_id,
            model_call_id=model_call_id,
            work_unit_id=work_unit_id,
            input_bound=input_bound,
            output_max=output_max,
            amount=amount,
            prepared_request_digest=prepared_request_digest,
            capability_ref=capability.capability_id,
            state=ReservationState.ACTIVE,
        )
        self._reservations[reservation.reservation_id] = reservation
        self._calls[model_call_id] = reservation.reservation_id
        self._append(account_id, LedgerEntryType.RESERVATION_CREATED, amount)
        return reservation

    def release_unsent_reservation(self, reservation_id: str) -> BudgetReservation:
        reservation = self._reservation(reservation_id)
        if reservation.state is not ReservationState.ACTIVE:
            raise ValueError("reservation already settled")
        updated = replace(reservation, state=ReservationState.RELEASED)
        self._reservations[reservation_id] = updated
        self._append(
            reservation.account_id,
            LedgerEntryType.RESERVATION_RELEASED,
            reservation.amount,
            reservation_amount=reservation.amount,
        )
        return updated

    def settle_known_usage(
        self, reservation_id: str, usage: NormalizedActualUsage
    ) -> BudgetSettlement:
        reservation = self._reservation(reservation_id)
        if reservation.state is not ReservationState.ACTIVE:
            raise ValueError("reservation already settled")
        actual = usage.total
        overage = max(0, actual - reservation.amount)
        updated = replace(
            reservation,
            state=ReservationState.SETTLED_KNOWN,
            usage_state=UsageState.KNOWN,
            actual_usage=actual,
        )
        self._reservations[reservation_id] = updated
        self._append(
            reservation.account_id,
            LedgerEntryType.USAGE_SETTLED,
            actual,
            reservation_amount=reservation.amount,
        )
        if overage:
            self._append(
                reservation.account_id,
                LedgerEntryType.USAGE_OVERAGE_RECORDED,
                overage,
            )
            account = self._account(reservation.account_id)
            frozen = replace(
                account,
                state=BudgetAccountState.FROZEN_OVERAGE,
                active_freeze_id=str(uuid4()),
                revision=account.revision + 1,
            )
            self._accounts[account.account_id] = frozen
            self._append(account.account_id, LedgerEntryType.FREEZE_APPLIED, 0)
        return BudgetSettlement(updated, actual, overage)

    def settle_uncertain_usage(
        self,
        reservation_id: str,
        *,
        usage_state: UsageState,
        reported_usage: int | None = None,
        reason: str | None = None,
    ) -> BudgetReservation:
        reservation = self._reservation(reservation_id)
        if reservation.state is not ReservationState.ACTIVE:
            raise ValueError("reservation already settled")
        if usage_state not in (UsageState.MISSING, UsageState.UNTRUSTED):
            raise ValueError("usage must be missing or untrusted")
        if reported_usage is not None and reported_usage < 0:
            raise ValueError("reported usage must be non-negative")
        amount = max(reservation.amount, reported_usage or 0)
        updated = replace(
            reservation,
            state=ReservationState.SETTLED_UNCERTAIN,
            usage_state=usage_state,
            actual_usage=amount,
        )
        self._reservations[reservation_id] = updated
        self._append(
            reservation.account_id,
            LedgerEntryType.UNKNOWN_COMMITTED,
            amount,
            reservation_amount=reservation.amount,
            usage_state=usage_state,
        )
        if amount > reservation.amount:
            account = self._account(reservation.account_id)
            self._accounts[account.account_id] = replace(
                account, pending_revalidation=True, revision=account.revision + 1
            )
        return updated

    def add_authorization(self, account_id: str, amount: int) -> BudgetAccount:
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid_budget")
        account = self._account(account_id)
        summary = self.get_summary(account_id)
        if summary.authorized + amount > MAX_AUTHORIZATION:
            raise ValueError("authorization_limit_exceeded")
        self._append(account_id, LedgerEntryType.AUTHORIZATION_ADDED, amount)
        updated = replace(account, revision=account.revision + 1)
        self._accounts[account_id] = updated
        return updated

    def evaluate_unfreeze(
        self,
        account_id: str,
        *,
        capability: ProviderHardBudgetCapability,
        explicit_resume: bool,
    ) -> bool:
        account = self._account(account_id)
        if not explicit_resume or not capability.valid:
            return False
        if capability.capability_id != account.provider_capability_baseline_ref:
            return False
        summary = self.get_summary(account_id)
        if summary.authorized < summary.committed:
            return False
        if (
            account.state is BudgetAccountState.FROZEN_OVERAGE
            or account.pending_revalidation
        ):
            self._accounts[account_id] = replace(
                account,
                state=BudgetAccountState.OPEN,
                active_freeze_id=None,
                pending_revalidation=False,
                revision=account.revision + 1,
            )
            self._append(account_id, LedgerEntryType.FREEZE_CLEARED, 0)
        return True

    def get_summary(
        self, account_id: str, ledger_version: int | None = None
    ) -> BudgetSummary:
        account = self._account_or_task(account_id)
        entries = tuple(self._entries[account.account_id])
        if ledger_version is not None:
            if ledger_version < 0 or ledger_version > account.last_sequence:
                raise ValueError("budget ledger version not available")
            entries = tuple(
                entry for entry in entries if entry.sequence <= ledger_version
            )
        return BudgetLedger.project(
            account.task_id,
            account.state,
            entries,
            pending_revalidation=account.pending_revalidation,
        )

    def _account_or_task(self, identifier: str) -> BudgetAccount:
        if identifier in self._accounts:
            return self._accounts[identifier]
        matches = [
            account
            for account in self._accounts.values()
            if account.task_id == identifier
        ]
        if len(matches) == 1:
            return matches[0]
        raise ValueError("budget account not found")

    def _account(self, account_id: str) -> BudgetAccount:
        try:
            return self._accounts[account_id]
        except KeyError as exc:
            raise ValueError("budget account not found") from exc

    def _reservation(self, reservation_id: str) -> BudgetReservation:
        try:
            return self._reservations[reservation_id]
        except KeyError as exc:
            raise ValueError("reservation not found") from exc

    def _append(
        self,
        account_id: str,
        entry_type: LedgerEntryType,
        amount: int,
        *,
        reservation_amount: int = 0,
        usage_state: UsageState | None = None,
    ) -> BudgetLedgerEntry:
        account = self._account(account_id)
        entry = BudgetLedger.entry(
            entry_type,
            amount,
            account.last_sequence + 1,
            reservation_amount=reservation_amount,
            usage_state=usage_state,
        )
        self._entries[account_id].append(entry)
        self._accounts[account_id] = replace(
            account,
            last_sequence=entry.sequence,
            revision=account.revision + 1,
        )
        return entry
