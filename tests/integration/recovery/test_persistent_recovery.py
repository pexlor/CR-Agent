from __future__ import annotations

import importlib
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_review_agent.application.dto import (
    ReviewRunResult,
    StartReviewCommand,
    TraceEventView,
)
from code_review_agent.application.orchestration import (
    ReviewDependencies,
    ReviewOrchestrator,
)
from code_review_agent.application.persistence import ReviewStateStore
from code_review_agent.application.task_service import LocalDiffReviewService
from code_review_agent.bootstrap import ConfiguredRuntime
from code_review_agent.config import CliConfig
from code_review_agent.domain.budget.models import (
    BudgetAccountState,
    BudgetReservation,
    NormalizedActualUsage,
    ProviderHardBudgetCapability,
    ReservationDenied,
    UsageState,
)
from code_review_agent.domain.budget.service import BudgetService
from code_review_agent.domain.execution.execution_models import (
    CoverageImpact,
    ModelAttemptOutcomeKind,
    ModelCallAttempt,
    WorkUnitExecutionResult,
    WorkUnitExecutionState,
)
from code_review_agent.domain.execution.models import (
    ModelCallState,
    ProviderState,
    ResponseState,
)


@dataclass(frozen=True)
class _RecoveryUnit:
    work_unit_id: str
    execution_rank: int


def _recovery_execution(
    work_unit_id: str,
    *,
    task_id: str,
    state: WorkUnitExecutionState = WorkUnitExecutionState.SUCCEEDED,
    execution_id: str = "execution",
    attempt_number: int = 1,
) -> WorkUnitExecutionResult:
    unknown = state is WorkUnitExecutionState.UNKNOWN
    model_attempt = (
        ModelCallAttempt(
            model_call_id=f"model-{execution_id}",
            work_unit_id=work_unit_id,
            execution_id=execution_id,
            provider_id="provider",
            model_id="model",
            request_ref=None,
            response_ref=None,
            reservation_id=f"reservation-{execution_id}",
        )
        if unknown
        else None
    )
    return WorkUnitExecutionResult(
        execution_id=execution_id,
        task_id=task_id,
        work_unit_id=work_unit_id,
        plan_id="plan-1",
        attempt_number=attempt_number,
        state=state,
        coverage_impact=CoverageImpact.FULLY_COVERED,
        tool_attempts=(),
        model_attempt=model_attempt,
        model_outcome_kind=(
            ModelAttemptOutcomeKind.UNKNOWN
            if unknown
            else ModelAttemptOutcomeKind.NOT_ATTEMPTED
        ),
        candidates=(),
    )


class _InterruptingSteps:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.task_id = "task-units"
        self.calls: list[str] = []
        self.comments_seen: tuple[str, ...] = ()

    def normalize(self, command: object) -> object:
        self.calls.append("normalize")
        return SimpleNamespace(input_digest="input-digest")

    def plan(self, normalized: object) -> object:
        self.calls.append("plan")
        return SimpleNamespace(
            work_units=(
                _RecoveryUnit("unit-1", 1),
                _RecoveryUnit("unit-2", 2),
                _RecoveryUnit("unit-3", 3),
            )
        )

    async def execute(self, unit: _RecoveryUnit) -> WorkUnitExecutionResult:
        self.calls.append(unit.work_unit_id)
        if unit.work_unit_id == self.fail_on:
            raise RuntimeError("injected interruption")
        return _recovery_execution(unit.work_unit_id, task_id=self.task_id)

    def consolidate(
        self, normalized: object, plan: object, executions: tuple[object, ...]
    ) -> object:
        self.comments_seen = tuple(
            f"comment for {item.work_unit_id}" for item in executions  # type: ignore[attr-defined]
        )
        return SimpleNamespace()

    def snapshot(
        self,
        command: object,
        normalized: object,
        plan: object,
        executions: tuple[object, ...],
        finding_set: object,
    ) -> object:
        return SimpleNamespace(result_state="complete_no_findings")

    def report(self, snapshot: object) -> object:
        return SimpleNamespace()

    def deliver(self, model: object, target: Path) -> object:
        return SimpleNamespace(path=target, content_digest="digest")


class _UnknownThenSuccessfulSteps(_InterruptingSteps):
    def __init__(self) -> None:
        super().__init__()
        self.task_id = "task-unknown"
        self.execution_ids: list[str] = []
        self.attempt_numbers: list[int] = []

    async def execute(self, unit: _RecoveryUnit) -> object:
        self.calls.append(unit.work_unit_id)
        attempt = len(self.execution_ids) + 1
        execution_id = f"execution-{attempt}"
        self.execution_ids.append(execution_id)
        self.attempt_numbers.append(attempt)
        return _recovery_execution(
            unit.work_unit_id,
            task_id=self.task_id,
            state=(
                WorkUnitExecutionState.UNKNOWN
                if attempt == 1
                else WorkUnitExecutionState.SUCCEEDED
            ),
            execution_id=execution_id,
            attempt_number=attempt,
        )


class _FailingCheckpointStore:
    def load_checkpoint(self, task_id: str, **bindings: str) -> object:
        raise ValueError("checkpoint_not_found")

    def save_checkpoint(self, **payload: object) -> object:
        raise OSError("disk full")


def _capability() -> ProviderHardBudgetCapability:
    return ProviderHardBudgetCapability(
        capability_id="capability-1",
        provider_id="provider",
        provider_version="1",
        model_id="model",
        origin="https://api.example.test",
        prepared_request_digest="digest",
        token_counted_request_digest="digest",
        max_output_enforced=True,
        usage_mapping_trusted=True,
        retries_disabled=True,
    )


def _reserve(
    budget: BudgetService,
    account_id: str,
    *,
    call_id: str,
    amount: int = 20,
) -> BudgetReservation | ReservationDenied:
    return budget.prepare_reservation(
        account_id,
        model_call_id=call_id,
        work_unit_id=f"unit-{call_id}",
        input_bound=amount // 2,
        output_max=amount - amount // 2,
        prepared_request_digest="digest",
        capability=_capability(),
    )


def test_review_state_store_survives_a_new_instance(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    result = ReviewRunResult(
        task_id="task-1",
        session_id="session-1",
        phase="completed",
        result_state="unknown",
        delivery_state="succeeded",
        report_path=Path("reports/task-1.md"),
        report_digest="digest",
        limitations=("unknown_execution",),
        trace=(TraceEventView(1, "executing", "phase advanced"),),
    )

    ReviewStateStore(database).save(result)
    loaded = ReviewStateStore(database).get("task-1")

    assert loaded == result


def test_review_state_store_records_pause_and_resume_requests(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")

    store.request_pause("task-1", "user_requested")
    assert store.is_paused("task-1")

    store.clear_pause("task-1")
    assert not store.is_paused("task-1")


def test_budget_restores_known_usage_and_active_reservation_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    budget = BudgetService(ReviewStateStore(database))
    account = budget.create_account(
        "task-budget-known", 100, capability_ref="capability-1"
    )
    settled = _reserve(budget, account.account_id, call_id="call-known")
    assert not isinstance(settled, ReservationDenied)
    budget.settle_known_usage(
        settled.reservation_id,
        NormalizedActualUsage(input_tokens=6, output_tokens=4),
    )
    active = _reserve(budget, account.account_id, call_id="call-active", amount=25)
    assert not isinstance(active, ReservationDenied)
    before = budget.get_summary(account.account_id)

    restarted = BudgetService(ReviewStateStore(database))
    restored = restarted.restore_account(
        "task-budget-known", 100, capability_ref="capability-1"
    )

    assert restored.account_id == account.account_id
    assert restarted.get_summary(restored.account_id) == before
    with pytest.raises(ValueError, match="reservation already exists"):
        _reserve(restarted, restored.account_id, call_id="call-active", amount=25)


def test_budget_restores_uncertain_usage_without_reauthorizing_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    budget = BudgetService(ReviewStateStore(database))
    account = budget.create_account(
        "task-budget-unknown", 100, capability_ref="capability-1"
    )
    reservation = _reserve(budget, account.account_id, call_id="call-unknown")
    assert not isinstance(reservation, ReservationDenied)
    budget.settle_uncertain_usage(
        reservation.reservation_id,
        usage_state=UsageState.MISSING,
        reason="provider outcome unknown",
    )
    before = budget.get_summary(account.account_id)

    restarted = BudgetService(ReviewStateStore(database))
    restored = restarted.restore_account(
        "task-budget-unknown", 100, capability_ref="capability-1"
    )

    assert restarted.get_summary(restored.account_id) == before
    assert before.uncertain_consumption == 20
    with pytest.raises(ValueError, match="budget authorization mismatch"):
        BudgetService(ReviewStateStore(database)).restore_account(
            "task-budget-unknown", 200, capability_ref="capability-1"
        )


def test_budget_restores_insufficient_and_frozen_state_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    budget = BudgetService(ReviewStateStore(database))
    account = budget.create_account(
        "task-budget-frozen", 30, capability_ref="capability-1"
    )
    reservation = _reserve(
        budget, account.account_id, call_id="call-overage", amount=20
    )
    assert not isinstance(reservation, ReservationDenied)
    budget.settle_known_usage(
        reservation.reservation_id,
        NormalizedActualUsage(input_tokens=20, output_tokens=10),
    )
    before = budget.get_summary(account.account_id)
    assert before.account_state is BudgetAccountState.FROZEN_OVERAGE

    restarted = BudgetService(ReviewStateStore(database))
    restored = restarted.restore_account(
        "task-budget-frozen", 30, capability_ref="capability-1"
    )
    denied = _reserve(restarted, restored.account_id, call_id="call-after-restart")

    assert isinstance(denied, ReservationDenied)
    assert denied.code == "budget_frozen"
    assert restarted.get_summary(restored.account_id) == before


@pytest.mark.asyncio
async def test_resume_after_restart_reuses_completed_units_and_comments(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    command = StartReviewCommand(
        "task-units", "safe diff", None, tmp_path / "review.md"
    )
    first_steps = _InterruptingSteps(fail_on="unit-3")
    first_service = LocalDiffReviewService(
        ReviewOrchestrator(), store=ReviewStateStore(database)
    )

    with pytest.raises(RuntimeError, match="injected interruption"):
        await first_service.start(command, ReviewDependencies(first_steps))

    restarted_steps = _InterruptingSteps()
    restarted_service = LocalDiffReviewService(
        ReviewOrchestrator(), store=ReviewStateStore(database)
    )
    result = await restarted_service.resume(
        command.task_id, ReviewDependencies(restarted_steps)
    )

    assert result.result_state == "complete_no_findings"
    assert restarted_steps.calls == ["normalize", "plan", "unit-3"]
    assert restarted_steps.comments_seen == (
        "comment for unit-1",
        "comment for unit-2",
        "comment for unit-3",
    )


def test_initial_checkpoint_is_saved_after_normalization_without_raw_secret(
    tmp_path: Path,
) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")
    secret = "sk-secret-material"

    store.save_checkpoint(
        task_id="task-safe",
        input_digest="input-digest",
        rules_config_digest="rules-digest",
        kind="input_normalized",
    )

    payload = store.load_checkpoint(
        "task-safe",
        input_digest="input-digest",
        rules_config_digest="rules-digest",
    )
    assert payload.completed_units == ()
    assert secret not in (tmp_path / "state.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )


def test_checkpoint_rejects_changed_input_or_rules_and_corruption(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    store.save_checkpoint(
        task_id="task-bound",
        input_digest="input-a",
        rules_config_digest="rules-a",
        kind="input_normalized",
    )

    with pytest.raises(ValueError, match="checkpoint_binding_mismatch"):
        store.load_checkpoint(
            "task-bound", input_digest="input-b", rules_config_digest="rules-a"
        )
    with pytest.raises(ValueError, match="checkpoint_binding_mismatch"):
        store.load_checkpoint(
            "task-bound", input_digest="input-a", rules_config_digest="rules-b"
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_checkpoints SET payload = 'broken' WHERE task_id = ?",
            ("task-bound",),
        )
    with pytest.raises(ValueError, match="checkpoint_corrupt"):
        store.load_checkpoint(
            "task-bound", input_digest="input-a", rules_config_digest="rules-a"
        )


def test_checkpoint_expiry_initialization_and_cleanup_remove_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    store.save_checkpoint(
        task_id="task-expired",
        input_digest="input",
        rules_config_digest="rules",
        kind="input_normalized",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    restarted = ReviewStateStore(database)
    with pytest.raises(ValueError, match="checkpoint_expired"):
        restarted.load_checkpoint(
            "task-expired", input_digest="input", rules_config_digest="rules"
        )

    result = ReviewRunResult(
        task_id="task-clean",
        session_id="session",
        phase="completed",
        result_state="complete_no_findings",
        delivery_state="succeeded",
        report_path=None,
        report_digest=None,
    )
    restarted.save(result)
    restarted.save_checkpoint(
        task_id="task-clean",
        input_digest="input",
        rules_config_digest="rules",
        kind="input_normalized",
    )
    restarted.cleanup("task-clean")
    with pytest.raises(ValueError, match="checkpoint_not_found"):
        restarted.load_checkpoint(
            "task-clean", input_digest="input", rules_config_digest="rules"
        )


@pytest.mark.asyncio
async def test_unknown_execution_is_persisted_separately_and_retry_needs_confirmation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    command = StartReviewCommand(
        "task-unknown", "safe diff", None, tmp_path / "review.md"
    )
    first_steps = _UnknownThenSuccessfulSteps()
    first_service = LocalDiffReviewService(ReviewOrchestrator(), store=store)

    first = await first_service.start(command, ReviewDependencies(first_steps))
    checkpoint = store.load_checkpoint(
        command.task_id,
        input_digest="input-digest",
        rules_config_digest=ReviewOrchestrator._rules_digest(first_steps, command),
    )

    assert first.result_state == "unknown"
    assert checkpoint.completed_units == ()
    assert len(checkpoint.unknown_units) == 1
    with pytest.raises(ValueError, match="unknown_retry_confirmation_required"):
        await LocalDiffReviewService(
            ReviewOrchestrator(), store=ReviewStateStore(database)
        ).resume(command.task_id, ReviewDependencies(first_steps))

    retry_steps = _UnknownThenSuccessfulSteps()
    retry_steps.execution_ids.append("historical-placeholder")
    resumed = await LocalDiffReviewService(
        ReviewOrchestrator(), store=ReviewStateStore(database)
    ).resume(
        command.task_id,
        ReviewDependencies(retry_steps),
        confirm_unknown_retry=True,
    )
    after = ReviewStateStore(database).load_checkpoint(
        command.task_id,
        input_digest="input-digest",
        rules_config_digest=ReviewOrchestrator._rules_digest(retry_steps, command),
    )

    assert resumed.result_state == "complete_no_findings"
    assert retry_steps.execution_ids[-1] != first_steps.execution_ids[0]
    assert dict(after.completed_units)["unit-1"].state == "succeeded"
    assert dict(after.unknown_units)["unit-1"].execution_id == "execution-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["checkpoint_expired", "checkpoint_corrupt"])
async def test_resume_rejects_unusable_checkpoint_instead_of_creating_new(
    tmp_path: Path, failure: str
) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    command = StartReviewCommand(
        f"task-{failure}", "safe diff", None, tmp_path / "review.md"
    )
    store.save_command(command, command.provider, command.model, command.budget_tokens)
    store.save_checkpoint(
        task_id=command.task_id,
        input_digest="input-digest",
        rules_config_digest=ReviewOrchestrator._rules_digest(
            _InterruptingSteps(), command
        ),
        kind="input_normalized",
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=1)
            if failure == "checkpoint_expired"
            else None
        ),
    )
    if failure == "checkpoint_corrupt":
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE review_checkpoints SET payload = 'broken' WHERE task_id = ?",
                (command.task_id,),
            )

    with pytest.raises(ValueError, match=failure):
        await LocalDiffReviewService(
            ReviewOrchestrator(), store=ReviewStateStore(database)
        ).resume(command.task_id, ReviewDependencies(_InterruptingSteps()))


@pytest.mark.asyncio
async def test_checkpoint_write_failure_is_explicitly_partial(tmp_path: Path) -> None:
    command = StartReviewCommand(
        "task-write-failure", "safe diff", None, tmp_path / "review.md"
    )

    result = await ReviewOrchestrator().run(
        command,
        ReviewDependencies(
            _InterruptingSteps(), checkpoint_store=_FailingCheckpointStore()
        ),
    )

    assert result.result_state == "partial"
    assert result.limitations == ("checkpoint_persistence_failed",)


def test_checkpoint_round_trip_preserves_strenum_field_types(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = ReviewStateStore(database)
    call_state = ModelCallState(
        provider_state=ProviderState.SUCCEEDED,
        response_state=ResponseState.ACCEPTED,
    )

    store.save_checkpoint(
        task_id="task-enum-roundtrip",
        input_digest="input",
        rules_config_digest="rules",
        kind="review_unit_completed",
        work_unit_id="unit-1",
        execution=call_state,
    )

    loaded = ReviewStateStore(database).load_checkpoint(
        "task-enum-roundtrip", input_digest="input", rules_config_digest="rules"
    )
    restored = dict(loaded.completed_units)["unit-1"]

    assert type(restored.provider_state) is ProviderState
    assert type(restored.response_state) is ResponseState
    assert restored.provider_state is ProviderState.SUCCEEDED
    assert restored.response_state is ResponseState.ACCEPTED


def test_checkpoint_rejects_repository_type_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted_imports: list[str] = []

    def reject_import(module_name: str) -> object:
        attempted_imports.append(module_name)
        raise AssertionError("repository module import attempted")

    monkeypatch.setattr(importlib, "import_module", reject_import)

    with pytest.raises(ValueError, match="checkpoint_corrupt"):
        ReviewStateStore._decode_checkpoint_value(
            {
                "__dataclass__": "test_repository_payload:Payload",
                "fields": {},
            }
        )

    assert attempted_imports == []


def test_resume_rejects_changed_model_price_config(tmp_path: Path) -> None:
    config = CliConfig(state_database=tmp_path / "state.sqlite3")
    command = StartReviewCommand(
        "task-price-change",
        (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        None,
        tmp_path / "report.md",
    )
    ConfiguredRuntime(config).review(
        command, "local", "deterministic", "request-1"
    )
    changed_price = replace(
        config, price_per_million_tokens_cny=Decimal("25.00")
    )

    with pytest.raises(ValueError, match="checkpoint_binding_mismatch"):
        ConfiguredRuntime(changed_price).resume(command.task_id)
