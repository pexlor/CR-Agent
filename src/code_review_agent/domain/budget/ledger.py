"""Append-only budget ledger projection."""

from __future__ import annotations

from uuid import uuid4

from code_review_agent.domain.budget.models import (
    BudgetAccountState,
    BudgetLedgerEntry,
    BudgetSummary,
    LedgerEntryType,
    UsageState,
)


class BudgetLedger:
    """Pure helpers for creating entries and projecting a ledger prefix."""

    @staticmethod
    def entry(
        entry_type: LedgerEntryType,
        amount: int,
        sequence: int,
        *,
        reservation_amount: int = 0,
        usage_state: UsageState | None = None,
    ) -> BudgetLedgerEntry:
        return BudgetLedgerEntry(
            entry_id=str(uuid4()),
            entry_type=entry_type,
            amount=amount,
            sequence=sequence,
            reservation_amount=reservation_amount,
            usage_state=usage_state,
        )

    @staticmethod
    def project(
        task_id: str,
        account_state: BudgetAccountState,
        entries: tuple[BudgetLedgerEntry, ...],
        *,
        pending_revalidation: bool = False,
    ) -> BudgetSummary:
        authorized = sum(
            entry.amount
            for entry in entries
            if entry.entry_type
            in (
                LedgerEntryType.AUTHORIZATION_INITIAL,
                LedgerEntryType.AUTHORIZATION_ADDED,
            )
        )
        known = sum(
            entry.amount
            for entry in entries
            if entry.entry_type is LedgerEntryType.USAGE_SETTLED
        )
        uncertain = sum(
            entry.amount
            for entry in entries
            if entry.entry_type is LedgerEntryType.UNKNOWN_COMMITTED
        )
        active = sum(
            entry.amount
            for entry in entries
            if entry.entry_type is LedgerEntryType.RESERVATION_CREATED
        )
        active -= sum(
            entry.reservation_amount
            for entry in entries
            if entry.entry_type
            in (
                LedgerEntryType.RESERVATION_RELEASED,
                LedgerEntryType.USAGE_SETTLED,
                LedgerEntryType.UNKNOWN_COMMITTED,
            )
        )
        active = max(0, active)
        released = sum(
            entry.amount
            for entry in entries
            if entry.entry_type is LedgerEntryType.RESERVATION_RELEASED
        )
        overage = sum(
            entry.amount
            for entry in entries
            if entry.entry_type is LedgerEntryType.USAGE_OVERAGE_RECORDED
        )
        committed = known + uncertain + active
        remaining = max(0, authorized - committed)
        deficit = max(0, committed - authorized)
        spendable = (
            remaining
            if account_state is BudgetAccountState.OPEN and not pending_revalidation
            else 0
        )
        return BudgetSummary(
            task_id=task_id,
            ledger_version=max((entry.sequence for entry in entries), default=0),
            authorized=authorized,
            known_consumption=known,
            uncertain_consumption=uncertain,
            active_reservations=active,
            released_reservations=released,
            overage=overage,
            remaining_budget=remaining,
            authorization_deficit=deficit,
            spendable_budget=spendable,
            account_state=account_state,
            pending_revalidation=pending_revalidation,
        )
