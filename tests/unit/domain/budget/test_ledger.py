from hypothesis import given, strategies as st

from code_review_agent.domain.budget.ledger import BudgetLedger
from code_review_agent.domain.budget.models import BudgetAccountState, LedgerEntryType


@given(
    authorization=st.integers(min_value=1, max_value=1_000_000),
    known=st.integers(min_value=0, max_value=1_000_000),
    uncertain=st.integers(min_value=0, max_value=1_000_000),
    active=st.integers(min_value=0, max_value=1_000_000),
)
def test_projection_formula_never_returns_negative_public_balances(
    authorization: int,
    known: int,
    uncertain: int,
    active: int,
) -> None:
    entries = (
        BudgetLedger.entry(LedgerEntryType.AUTHORIZATION_INITIAL, authorization, 1),
        BudgetLedger.entry(LedgerEntryType.USAGE_SETTLED, known, 2, reservation_amount=0),
        BudgetLedger.entry(LedgerEntryType.UNKNOWN_COMMITTED, uncertain, 3, reservation_amount=0),
        BudgetLedger.entry(LedgerEntryType.RESERVATION_CREATED, active, 4),
    )

    summary = BudgetLedger.project("task", BudgetAccountState.OPEN, entries)

    assert summary.committed == known + uncertain + active
    assert summary.remaining_budget == max(0, authorization - summary.committed)
    assert summary.authorization_deficit == max(0, summary.committed - authorization)
    assert summary.spendable_budget >= 0


def test_overage_is_disclosed_without_double_counting_consumption() -> None:
    entries = (
        BudgetLedger.entry(LedgerEntryType.AUTHORIZATION_INITIAL, 100, 1),
        BudgetLedger.entry(LedgerEntryType.RESERVATION_CREATED, 80, 2),
        BudgetLedger.entry(LedgerEntryType.USAGE_SETTLED, 120, 3, reservation_amount=80),
        BudgetLedger.entry(LedgerEntryType.USAGE_OVERAGE_RECORDED, 40, 4),
    )

    summary = BudgetLedger.project("task", BudgetAccountState.FROZEN_OVERAGE, entries)

    assert summary.known_consumption == 120
    assert summary.overage == 40
    assert summary.committed == 120
    assert summary.authorization_deficit == 20
    assert summary.spendable_budget == 0
