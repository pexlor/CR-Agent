import pytest

from code_review_agent.domain.budget.models import (
    BudgetAccountState,
    BudgetReservation,
    NormalizedActualUsage,
    ReservationState,
    TokenAmount,
)


def test_token_amount_is_non_negative_and_authorization_is_positive() -> None:
    assert int(TokenAmount(0)) == 0
    assert int(TokenAmount.authorization(50_000)) == 50_000
    with pytest.raises(ValueError):
        TokenAmount(-1)
    with pytest.raises(ValueError):
        TokenAmount.authorization(0)


def test_usage_and_reservation_validate_non_negative_values() -> None:
    usage = NormalizedActualUsage(input_tokens=10, output_tokens=5)
    assert usage.total == 15
    with pytest.raises(ValueError):
        NormalizedActualUsage(input_tokens=-1, output_tokens=0)
    with pytest.raises(ValueError):
        BudgetReservation(
            reservation_id="reservation",
            account_id="account",
            task_id="task",
            model_call_id="call",
            work_unit_id="unit",
            input_bound=0,
            output_max=0,
            amount=0,
            prepared_request_digest="digest",
            capability_ref="capability",
            state=ReservationState.ACTIVE,
        )


def test_budget_states_are_explicit() -> None:
    assert {state.value for state in BudgetAccountState} == {
        "open",
        "frozen_overage",
        "closed",
    }
