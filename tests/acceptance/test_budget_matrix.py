from __future__ import annotations

from code_review_agent.domain.budget.models import (
    BudgetAccountState,
    NormalizedActualUsage,
    ProviderHardBudgetCapability,
    ReservationDenied,
)
from code_review_agent.domain.budget.service import BudgetService


def capability() -> ProviderHardBudgetCapability:
    return ProviderHardBudgetCapability(
        capability_id="capability-1",
        provider_id="provider",
        provider_version="1",
        model_id="model",
        origin="https://api.example.test",
        prepared_request_digest="request-digest",
        token_counted_request_digest="request-digest",
        max_output_enforced=True,
        usage_mapping_trusted=True,
        retries_disabled=True,
        valid=True,
    )


def test_budget_matrix_rejects_new_call_after_overage() -> None:
    service = BudgetService()
    account = service.create_account(
        "acceptance-budget", 50, capability_ref="capability-1"
    )
    reservation = service.prepare_reservation(
        account.account_id,
        model_call_id="call-1",
        work_unit_id="unit-1",
        input_bound=20,
        output_max=20,
        prepared_request_digest="request-digest",
        capability=capability(),
    )
    assert not isinstance(reservation, ReservationDenied)
    service.settle_known_usage(
        reservation.reservation_id,
        NormalizedActualUsage(input_tokens=40, output_tokens=20),
    )

    denied = service.prepare_reservation(
        account.account_id,
        model_call_id="call-2",
        work_unit_id="unit-2",
        input_bound=1,
        output_max=1,
        prepared_request_digest="request-digest",
        capability=capability(),
    )

    assert isinstance(denied, ReservationDenied)
    assert (
        service.get_summary(account.account_id).account_state
        is BudgetAccountState.FROZEN_OVERAGE
    )
