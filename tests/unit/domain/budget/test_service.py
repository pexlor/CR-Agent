import pytest

from code_review_agent.domain.budget.models import (
    BudgetAccountState,
    NormalizedActualUsage,
    ProviderHardBudgetCapability,
    ReservationDenied,
    ReservationState,
    UsageState,
)
from code_review_agent.domain.budget.service import BudgetService


def capability(*, valid: bool = True, digest: str = "request-digest") -> ProviderHardBudgetCapability:
    return ProviderHardBudgetCapability(
        capability_id="capability-1",
        provider_id="provider",
        provider_version="1",
        model_id="model",
        origin="https://api.example.test",
        prepared_request_digest=digest,
        token_counted_request_digest=digest,
        max_output_enforced=True,
        usage_mapping_trusted=True,
        retries_disabled=True,
        valid=valid,
    )


def test_reservation_uses_input_plus_output_and_insufficient_budget_does_not_freeze() -> None:
    service = BudgetService()
    account = service.create_account("task-1", 100, capability_ref="capability-1")

    reservation = service.prepare_reservation(
        account.account_id,
        model_call_id="call-1",
        work_unit_id="unit-1",
        input_bound=30,
        output_max=40,
        prepared_request_digest="request-digest",
        capability=capability(),
    )
    assert not isinstance(reservation, ReservationDenied)
    assert reservation.amount == 70

    denied = service.prepare_reservation(
        account.account_id,
        model_call_id="call-2",
        work_unit_id="unit-2",
        input_bound=20,
        output_max=20,
        prepared_request_digest="request-digest",
        capability=capability(),
    )
    assert isinstance(denied, ReservationDenied)
    assert denied.code == "insufficient_budget"
    assert service.get_summary(account.account_id).account_state is BudgetAccountState.OPEN


def test_model_call_has_one_reservation_and_retry_uses_new_call() -> None:
    service = BudgetService()
    account = service.create_account("task-1", 200, capability_ref="capability-1")
    service.prepare_reservation(
        account.account_id,
        model_call_id="call-1",
        work_unit_id="unit-1",
        input_bound=20,
        output_max=20,
        prepared_request_digest="request-digest",
        capability=capability(),
    )

    with pytest.raises(ValueError):
        service.prepare_reservation(
            account.account_id,
            model_call_id="call-1",
            work_unit_id="unit-1",
            input_bound=20,
            output_max=20,
            prepared_request_digest="request-digest",
            capability=capability(),
        )
    retry = service.prepare_reservation(
        account.account_id,
        model_call_id="call-2",
        work_unit_id="unit-1",
        input_bound=20,
        output_max=20,
        prepared_request_digest="request-digest",
        capability=capability(),
    )
    assert not isinstance(retry, ReservationDenied)


def test_known_usage_settles_once_and_overage_freezes() -> None:
    service = BudgetService()
    account = service.create_account("task-1", 100, capability_ref="capability-1")
    reservation = service.prepare_reservation(
        account.account_id,
        model_call_id="call-1",
        work_unit_id="unit-1",
        input_bound=20,
        output_max=30,
        prepared_request_digest="request-digest",
        capability=capability(),
    )
    assert not isinstance(reservation, ReservationDenied)

    settlement = service.settle_known_usage(
        reservation.reservation_id,
        NormalizedActualUsage(input_tokens=40, output_tokens=30),
    )
    assert settlement.overage == 20
    assert settlement.reservation.state is ReservationState.SETTLED_KNOWN
    summary = service.get_summary(account.account_id)
    assert summary.known_consumption == 70
    assert summary.overage == 20
    assert summary.account_state is BudgetAccountState.FROZEN_OVERAGE
    with pytest.raises(ValueError):
        service.settle_known_usage(
            reservation.reservation_id,
            NormalizedActualUsage(input_tokens=1, output_tokens=1),
        )


def test_unknown_and_untrusted_usage_are_conservative_and_mutually_exclusive() -> None:
    service = BudgetService()
    account = service.create_account("task-1", 200, capability_ref="capability-1")
    reservation = service.prepare_reservation(
        account.account_id,
        model_call_id="call-1",
        work_unit_id="unit-1",
        input_bound=20,
        output_max=30,
        prepared_request_digest="request-digest",
        capability=capability(),
    )
    assert not isinstance(reservation, ReservationDenied)

    service.settle_uncertain_usage(
        reservation.reservation_id,
        usage_state=UsageState.UNTRUSTED,
        reported_usage=80,
    )
    summary = service.get_summary(account.account_id)
    assert summary.known_consumption == 0
    assert summary.uncertain_consumption == 80
    assert summary.pending_revalidation is True


def test_add_authorization_preserves_history_and_does_not_auto_unfreeze() -> None:
    service = BudgetService()
    account = service.create_account("task-1", 50, capability_ref="capability-1")
    reservation = service.prepare_reservation(
        account.account_id,
        model_call_id="call-1",
        work_unit_id="unit-1",
        input_bound=20,
        output_max=30,
        prepared_request_digest="request-digest",
        capability=capability(),
    )
    assert not isinstance(reservation, ReservationDenied)
    service.settle_known_usage(
        reservation.reservation_id,
        NormalizedActualUsage(input_tokens=40, output_tokens=30),
    )

    service.add_authorization(account.account_id, 30)
    summary = service.get_summary(account.account_id)
    assert summary.authorized == 80
    assert summary.known_consumption == 70
    assert summary.account_state is BudgetAccountState.FROZEN_OVERAGE
    assert service.evaluate_unfreeze(
        account.account_id,
        capability=capability(),
        explicit_resume=True,
    )
    assert service.get_summary(account.account_id).account_state is BudgetAccountState.OPEN
