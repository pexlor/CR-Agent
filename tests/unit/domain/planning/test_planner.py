"""Deterministic review planner behaviour."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.common.time import FixedClock
from code_review_agent.domain.input.models import NormalizedInput
from code_review_agent.domain.input.service import InputService
from code_review_agent.domain.planning.models import (
    ModelCapacitySummary,
    PlannedDisposition,
    PlanningStrategy,
    ReviewPlan,
    ToolFailureImpact,
    WorkUnitKind,
)
from code_review_agent.domain.planning.planner import PlanningRequest, ReviewPlanner
from code_review_agent.domain.security.models import ScanResult
from code_review_agent.domain.security.policy import SecurityPolicy
from code_review_agent.domain.security.service import SecurityService
from code_review_agent.domain.task.models import InputBinding, TaskSpec
from code_review_agent.ports.tools import FixedToolReference, ToolRegistrySnapshot
from tests.unit.tools.conftest import tool_manifest

FIXTURES = Path("tests/fixtures/diffs")


class _NoOpScanner:
    def scan(self, content, descriptor, policy):  # type: ignore[no-untyped-def]
        return ScanResult.complete((), policy.detector_manifest)


def _policy() -> SecurityPolicy:
    return SecurityPolicy(
        policy_id="planning-test",
        policy_version=1,
        schema_version=1,
        sensitive_categories=frozenset(),
        detector_manifest=("noop@1",),
        action_matrix={},
        purpose_transitions=frozenset(),
    )


def _normalize(text: str, *, task_id: str = "task-1") -> NormalizedInput:
    from code_review_agent.adapters.input.plain_diff import PlainDiffProvider

    security = SecurityService(_NoOpScanner(), policy=_policy())
    service = InputService(
        PlainDiffProvider(security),
        security,
        clock=FixedClock(datetime(2026, 9, 11, 8, 0, tzinfo=UTC)),
    )
    return service.normalize_plain_diff(task_id=task_id, text=text)


def _spec() -> TaskSpec:
    return TaskSpec(
        spec_id="spec-1",
        input_intent="plain_diff",
        provider_id="fake",
        provider_version="1",
        model_id="fake-model",
        provider_origin="https://example.invalid",
        ruleset_id="ruleset",
        ruleset_version="1",
        tools=("dangerous-python-execution@1.0.0",),
        security_policy_id="planning-test",
        security_policy_version=1,
        config_digest="a" * 64,
        credential_alias="alias",
        budget_account_id="budget-1",
    )


def _capacity(
    *, context_token_limit: int = 100_000, max_output_tokens: int = 4_096
) -> ModelCapacitySummary:
    return ModelCapacitySummary(
        provider_id="fake",
        provider_version="1",
        model_id="fake-model",
        context_token_limit=context_token_limit,
        max_output_tokens=max_output_tokens,
        token_counting_version="v1",
    )


def _catalog() -> ToolRegistrySnapshot:
    return ToolRegistrySnapshot(
        (
            FixedToolReference(
                tool_id="dangerous-python-execution",
                version="1.0.0",
                contract_version="1.0",
                origin="builtin",
                resource_digest="a" * 64,
                schema_digest="b" * 64,
                declaration_digest="c" * 64,
            ),
        )
    )


def _request(
    text: str, *, capacity: ModelCapacitySummary | None = None
) -> PlanningRequest:
    normalized = _normalize(text)
    return PlanningRequest(
        task_id="task-1",
        task_spec=_spec(),
        binding=normalized.binding,
        change_set=normalized.change_set,
        strategy=PlanningStrategy("file_first_v1", "1"),
        model_capacity=capacity or _capacity(),
        tool_catalog=_catalog(),
    )


def test_small_change_produces_one_file_work_unit() -> None:
    request = _request(FIXTURES.joinpath("basic.diff").read_text(encoding="utf-8"))

    result = ReviewPlanner().plan(request)

    plan = result.plan
    assert len(plan.work_units) == 1
    unit = plan.work_units[0]
    assert unit.kind is WorkUnitKind.FILE
    assert unit.execution_rank == 0
    planned_scopes = [
        scope
        for scope in plan.coverage_scopes
        if scope.disposition is PlannedDisposition.PLANNED
    ]
    assert len(planned_scopes) == 1
    assert unit.scope_ids == (planned_scopes[0].scope_id,)


def test_binary_file_is_unreviewable_and_produces_no_work_unit() -> None:
    request = _request(FIXTURES.joinpath("binary.diff").read_text(encoding="utf-8"))

    result = ReviewPlanner().plan(request)

    plan = result.plan
    assert plan.work_units == ()
    assert len(plan.coverage_scopes) == 1
    scope = plan.coverage_scopes[0]
    assert scope.disposition is PlannedDisposition.UNREVIEWABLE
    assert scope.reason_code == "binary_content"


def test_rename_without_hunks_is_no_review_required() -> None:
    request = _request(FIXTURES.joinpath("rename.diff").read_text(encoding="utf-8"))

    result = ReviewPlanner().plan(request)

    plan = result.plan
    assert plan.work_units == ()
    scope = plan.coverage_scopes[0]
    assert scope.disposition is PlannedDisposition.NO_REVIEW_REQUIRED


def test_pure_deletion_is_planned_with_old_side_only_range() -> None:
    request = _request(
        FIXTURES.joinpath("pure_delete.diff").read_text(encoding="utf-8")
    )

    result = ReviewPlanner().plan(request)

    plan = result.plan
    assert len(plan.work_units) == 1
    unit = plan.work_units[0]
    assert unit.range.new_start is None
    assert unit.range.old_start == 1


def test_every_planned_scope_is_covered_by_exactly_one_work_unit() -> None:
    request = _request(FIXTURES.joinpath("basic.diff").read_text(encoding="utf-8"))

    result = ReviewPlanner().plan(request)

    plan = result.plan
    covered = [scope_id for unit in plan.work_units for scope_id in unit.scope_ids]
    assert len(set(covered)) == len(covered)
    planned_ids = {
        scope.scope_id
        for scope in plan.coverage_scopes
        if scope.disposition is PlannedDisposition.PLANNED
    }
    assert set(covered) == planned_ids


def test_identical_fixed_inputs_produce_the_same_plan_fingerprint() -> None:
    text = FIXTURES.joinpath("basic.diff").read_text(encoding="utf-8")

    first = ReviewPlanner().plan(_request(text)).plan
    second = ReviewPlanner().plan(_request(text)).plan

    assert first.plan_fingerprint == second.plan_fingerprint
    assert first.plan_id == second.plan_id


def test_a_large_file_is_boxed_into_multiple_hunk_groups() -> None:
    padding = "x" * 400
    hunks = "\n".join(
        f"@@ -{i * 10 + 1},1 +{i * 10 + 1},1 @@\n-old{i}{padding}\n+new{i}{padding}"
        for i in range(20)
    )
    text = "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n" + hunks + "\n"
    request = _request(text, capacity=_capacity(context_token_limit=400))

    result = ReviewPlanner().plan(request)

    plan = result.plan
    assert len(plan.work_units) > 1
    for unit in plan.work_units:
        assert unit.kind in (WorkUnitKind.HUNK_GROUP, WorkUnitKind.LINE_BLOCK)


def test_execution_rank_does_not_affect_work_unit_identity() -> None:
    text = FIXTURES.joinpath("basic.diff").read_text(encoding="utf-8")
    plan = ReviewPlanner().plan(_request(text)).plan
    unit = plan.work_units[0]

    from dataclasses import replace

    reranked = replace(unit, execution_rank=unit.execution_rank + 5)

    assert reranked.fingerprint == unit.fingerprint
    assert reranked.work_unit_id == unit.work_unit_id


def test_mismatched_binding_task_id_is_rejected() -> None:
    normalized = _normalize(
        FIXTURES.joinpath("basic.diff").read_text(encoding="utf-8"), task_id="task-1"
    )
    bad_binding = InputBinding(
        binding_id=normalized.binding.binding_id,
        task_id="different-task",
        input_type=normalized.binding.input_type,
        object_identity=normalized.binding.object_identity,
        base_sha=None,
        head_sha=None,
        content_digest=normalized.binding.content_digest,
        completeness_digest=normalized.binding.completeness_digest,
        changeset_ref=normalized.binding.changeset_ref,
    )

    with pytest.raises(StableError) as captured:
        PlanningRequest(
            task_id="task-1",
            task_spec=_spec(),
            binding=bad_binding,
            change_set=normalized.change_set,
            strategy=PlanningStrategy("file_first_v1", "1"),
            model_capacity=_capacity(),
            tool_catalog=_catalog(),
        )

    assert captured.value.code == "changeset_integrity_error"


def test_empty_diff_produces_an_empty_plan() -> None:
    request = _request("")

    result = ReviewPlanner().plan(request)

    assert result.plan.work_units == ()
    assert result.plan.coverage_scopes == ()


def test_different_strategy_versions_change_work_unit_identity() -> None:
    text = FIXTURES.joinpath("basic.diff").read_text(encoding="utf-8")
    normalized = _normalize(text)

    def plan_with_strategy(version: str) -> ReviewPlan:
        request = PlanningRequest(
            task_id="task-1",
            task_spec=_spec(),
            binding=normalized.binding,
            change_set=normalized.change_set,
            strategy=PlanningStrategy("file_first_v1", version),
            model_capacity=_capacity(),
            tool_catalog=_catalog(),
        )
        return ReviewPlanner().plan(request).plan

    first = plan_with_strategy("1")
    second = plan_with_strategy("2")

    assert first.work_units[0].work_unit_id != second.work_units[0].work_unit_id
    assert first.work_units[0].fingerprint != second.work_units[0].fingerprint
    assert first.plan_fingerprint != second.plan_fingerprint


def test_a_hunk_too_large_for_any_line_block_is_unreviewable() -> None:
    huge_line = "x" * 100_000
    text = (
        "diff --git a/huge.py b/huge.py\n"
        "--- a/huge.py\n"
        "+++ b/huge.py\n"
        "@@ -1,1 +1,1 @@\n"
        f"-old\n"
        f"+{huge_line}\n"
    )
    request = _request(text, capacity=_capacity(context_token_limit=10))

    result = ReviewPlanner().plan(request)

    plan = result.plan
    assert plan.work_units == ()
    scope = next(
        scope
        for scope in plan.coverage_scopes
        if scope.scope_kind in ("hunk_group", "line_block")
    )
    assert scope.disposition is PlannedDisposition.UNREVIEWABLE
    assert scope.reason_code == "context_capacity_exceeded"


def test_plan_assigns_enabled_tools_matching_the_file_language() -> None:
    from code_review_agent.adapters.tools.registry import ToolRegistry
    from code_review_agent.domain.task.models import TaskSpec as Spec

    registry = ToolRegistry()
    declaration = registry.register_toml(
        tool_manifest(tool_id="dangerous-eval")
    )[0]
    text = FIXTURES.joinpath("basic.diff").read_text(encoding="utf-8")
    normalized = _normalize(text)
    spec = Spec(
        spec_id="spec-tools",
        input_intent="plain_diff",
        provider_id="fake",
        provider_version="1",
        model_id="fake-model",
        provider_origin="https://example.invalid",
        ruleset_id="ruleset",
        ruleset_version="1",
        tools=(f"{declaration.tool_id}@{declaration.version}",),
        security_policy_id="planning-test",
        security_policy_version=1,
        config_digest="a" * 64,
        credential_alias="alias",
        budget_account_id="budget-1",
    )
    request = PlanningRequest(
        task_id="task-1",
        task_spec=spec,
        binding=normalized.binding,
        change_set=normalized.change_set,
        strategy=PlanningStrategy("file_first_v1", "1"),
        model_capacity=_capacity(),
        tool_catalog=_catalog(),
        tool_declarations=(declaration,),
    )

    result = ReviewPlanner().plan(request)

    unit = result.plan.work_units[0]
    assert len(unit.tools) == 1
    selection = unit.tools[0]
    assert selection.fixed_reference.tool_id == "dangerous-eval"
    assert selection.applicable_rule_ids == ("dangerous-eval",)
    assert selection.failure_impact is ToolFailureImpact.EVIDENCE_DEGRADED
    assert selection.order == 0
    assert "python" in selection.applicability_reason


def test_plan_skips_tools_not_enabled_or_for_other_languages() -> None:
    from code_review_agent.adapters.tools.registry import ToolRegistry
    from code_review_agent.domain.task.models import TaskSpec as Spec

    registry = ToolRegistry()
    declaration = registry.register_toml(
        tool_manifest(tool_id="dangerous-eval")
    )[0]
    text = FIXTURES.joinpath("basic.diff").read_text(encoding="utf-8")
    normalized = _normalize(text)
    base = dict(
        spec_id="spec-tools-none",
        input_intent="plain_diff",
        provider_id="fake",
        provider_version="1",
        model_id="fake-model",
        provider_origin="https://example.invalid",
        ruleset_id="ruleset",
        ruleset_version="1",
        security_policy_id="planning-test",
        security_policy_version=1,
        config_digest="a" * 64,
        credential_alias="alias",
        budget_account_id="budget-1",
    )

    def plan_with(tools: tuple[str, ...]) -> ReviewPlan:
        request = PlanningRequest(
            task_id="task-1",
            task_spec=Spec(tools=tools, **base),
            binding=normalized.binding,
            change_set=normalized.change_set,
            strategy=PlanningStrategy("file_first_v1", "1"),
            model_capacity=_capacity(),
            tool_catalog=_catalog(),
            tool_declarations=(declaration,),
        )
        return ReviewPlanner().plan(request).plan

    not_enabled = plan_with(("other-tool@1.0.0",))
    assert not_enabled.work_units[0].tools == ()
