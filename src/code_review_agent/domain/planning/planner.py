"""Deterministic file-first / hunk-boxing / line-block review planner."""

from __future__ import annotations

from dataclasses import dataclass

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.common.errors import StableError
from code_review_agent.domain.input.models import ChangedFile, ChangeSet, Hunk, Line
from code_review_agent.domain.planning.models import (
    CapacityEstimate,
    CoverageScope,
    LineRange,
    ModelCapacitySummary,
    PlannedDisposition,
    PlanningStrategy,
    ReviewPlan,
    WorkUnit,
    WorkUnitKind,
)
from code_review_agent.domain.task.models import InputBinding, TaskSpec
from code_review_agent.ports.tools import ToolRegistrySnapshot

CAPACITY_ESTIMATOR_VERSION = "conservative_byte_v1"
_FIXED_OVERHEAD_TOKENS = 256
_BYTES_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    """Fixed, immutable inputs to one deterministic planning run."""

    task_id: str
    task_spec: TaskSpec
    binding: InputBinding
    change_set: ChangeSet
    strategy: PlanningStrategy
    model_capacity: ModelCapacitySummary
    tool_catalog: ToolRegistrySnapshot

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("planning request task_id is required")
        if self.task_id != self.binding.task_id:
            raise _planning_error("changeset_integrity_error")
        if self.change_set.sanitized_diff_ref.task_id != self.task_id:
            raise _planning_error("unsafe_artifact")
        if self.binding.content_digest != self.change_set.identity.content_digest:
            raise _planning_error("changeset_integrity_error")
        if (
            self.binding.completeness_digest
            != self.change_set.completeness.proof_digest
        ):
            raise _planning_error("changeset_integrity_error")


@dataclass(frozen=True, slots=True)
class PlanningResult:
    plan: ReviewPlan


class ReviewPlanner:
    """Converts a fixed ChangeSet into a coverage-conserving, frozen plan."""

    def plan(self, request: PlanningRequest) -> PlanningResult:
        change_set = request.change_set
        plan_id = "plan_" + sha256_digest(
            {
                "task_id": request.task_id,
                "binding_id": request.binding.binding_id,
                "change_set_digest": change_set.change_set_digest,
                "strategy": [
                    request.strategy.strategy_id,
                    request.strategy.strategy_version,
                ],
                "model_capacity": request.model_capacity.digest_payload(),
                "tool_catalog_digest": request.tool_catalog.catalog_digest,
            }
        )

        scopes: list[CoverageScope] = []
        work_units: list[WorkUnit] = []
        rank = 0
        for changed_file in _ordered_files(change_set.files):
            file_scopes, file_units, rank = self._plan_file(
                plan_id=plan_id,
                changed_file=changed_file,
                strategy=request.strategy,
                model_capacity=request.model_capacity,
                start_rank=rank,
            )
            scopes.extend(file_scopes)
            work_units.extend(file_units)

        plan = ReviewPlan(
            plan_id=plan_id,
            plan_version=1,
            task_id=request.task_id,
            input_binding_id=request.binding.binding_id,
            change_set_id=change_set.change_set_id,
            change_set_digest=change_set.change_set_digest,
            strategy=request.strategy,
            model_capacity=request.model_capacity,
            tool_catalog_digest=request.tool_catalog.catalog_digest,
            coverage_scopes=tuple(scopes),
            work_units=tuple(work_units),
        )
        return PlanningResult(plan=plan)

    def _plan_file(
        self,
        *,
        plan_id: str,
        changed_file: ChangedFile,
        strategy: PlanningStrategy,
        model_capacity: ModelCapacitySummary,
        start_rank: int,
    ) -> tuple[tuple[CoverageScope, ...], tuple[WorkUnit, ...], int]:
        rank = start_rank
        if changed_file.is_binary:
            scope = _unreviewable_file_scope(changed_file, reason_code="binary_content")
            return (scope,), (), rank
        if not changed_file.hunks:
            scope = _no_review_required_scope(changed_file)
            return (scope,), (), rank

        file_estimate = _estimate_hunks(changed_file.hunks)
        if _fits(file_estimate, model_capacity):
            file_scope = _file_scope(changed_file)
            unit = self._build_unit(
                plan_id=plan_id,
                changed_file=changed_file,
                kind=WorkUnitKind.FILE,
                hunks=changed_file.hunks,
                scope_ids=(file_scope.scope_id,),
                estimate=file_estimate,
                strategy=strategy,
                model_capacity=model_capacity,
                rank=rank,
            )
            return (
                (file_scope,),
                (unit,),
                rank + 1,
            )

        groups = _box_hunks(changed_file.hunks, model_capacity)
        scopes: list[CoverageScope] = []
        units: list[WorkUnit] = []
        for group in groups:
            if len(group) > 1 or _fits(_estimate_hunks(group), model_capacity):
                scope = _hunk_group_scope(changed_file, group)
                unit = self._build_unit(
                    plan_id=plan_id,
                    changed_file=changed_file,
                    kind=WorkUnitKind.HUNK_GROUP,
                    hunks=group,
                    scope_ids=(scope.scope_id,),
                    estimate=_estimate_hunks(group),
                    strategy=strategy,
                    model_capacity=model_capacity,
                    rank=rank,
                )
                scopes.append(scope)
                units.append(unit)
                rank += 1
                continue

            hunk = group[0]
            blocks = _line_blocks(hunk, model_capacity)
            if blocks is None:
                scopes.append(
                    _unreviewable_hunk_scope(
                        changed_file, hunk, reason_code="context_capacity_exceeded"
                    )
                )
                continue
            for block in blocks:
                scope = _line_block_scope(changed_file, hunk, block)
                estimate = _estimate_lines(len(block))
                unit = self._build_unit(
                    plan_id=plan_id,
                    changed_file=changed_file,
                    kind=WorkUnitKind.LINE_BLOCK,
                    hunks=(hunk,),
                    scope_ids=(scope.scope_id,),
                    estimate=estimate,
                    strategy=strategy,
                    model_capacity=model_capacity,
                    rank=rank,
                    line_subset=block,
                )
                scopes.append(scope)
                units.append(unit)
                rank += 1

        return tuple(scopes), tuple(units), rank

    def _build_unit(
        self,
        *,
        plan_id: str,
        changed_file: ChangedFile,
        kind: WorkUnitKind,
        hunks: tuple[Hunk, ...],
        scope_ids: tuple[str, ...],
        estimate: tuple[int, int],
        strategy: PlanningStrategy,
        model_capacity: ModelCapacitySummary,
        rank: int,
        line_subset: tuple[Line, ...] | None = None,
    ) -> WorkUnit:
        lines = (
            list(line_subset)
            if line_subset is not None
            else [line for hunk in hunks for line in hunk.lines]
        )
        content_refs = tuple(line.content_ref for line in lines)
        old_numbers = [
            line.old_line_number for line in lines if line.old_line_number is not None
        ]
        new_numbers = [
            line.new_line_number for line in lines if line.new_line_number is not None
        ]
        line_range = LineRange(
            old_start=min(old_numbers) if old_numbers else None,
            old_end=max(old_numbers) if old_numbers else None,
            new_start=min(new_numbers) if new_numbers else None,
            new_end=max(new_numbers) if new_numbers else None,
        )
        input_tokens, output_tokens = estimate
        work_unit_id = "work_unit_" + sha256_digest(
            {
                "plan_id": plan_id,
                "kind": kind.value,
                "file_id": changed_file.file_id,
                "scope_ids": list(scope_ids),
                "strategy": [strategy.strategy_id, strategy.strategy_version],
            }
        )
        return WorkUnit(
            work_unit_id=work_unit_id,
            plan_id=plan_id,
            kind=kind,
            file_id=changed_file.file_id,
            scope_ids=scope_ids,
            range=line_range,
            content_refs=content_refs,
            context_request=None,
            tools=(),
            capacity_estimate=CapacityEstimate(
                estimator_version=CAPACITY_ESTIMATOR_VERSION,
                estimated_input_tokens=input_tokens,
                estimated_output_tokens=output_tokens,
            ),
            strategy=strategy,
            model_capacity=model_capacity,
            execution_rank=rank,
        )


def _ordered_files(files: tuple[ChangedFile, ...]) -> tuple[ChangedFile, ...]:
    def key(item: ChangedFile) -> tuple[str, str, str, str]:
        return (
            item.new_path or "",
            item.old_path or "",
            item.change_type.value,
            item.file_id,
        )

    return tuple(sorted(files, key=key))


def _estimate_hunks(hunks: tuple[Hunk, ...]) -> tuple[int, int]:
    byte_count = sum(
        line.content_ref.end - line.content_ref.start
        for hunk in hunks
        for line in hunk.lines
    )
    input_tokens = _FIXED_OVERHEAD_TOKENS + (byte_count // _BYTES_PER_TOKEN) + 1
    output_tokens = max(1, len([line for hunk in hunks for line in hunk.lines]) // 2)
    return input_tokens, output_tokens


def _estimate_lines(line_count: int) -> tuple[int, int]:
    input_tokens = _FIXED_OVERHEAD_TOKENS + (line_count * 20 // _BYTES_PER_TOKEN) + 1
    output_tokens = max(1, line_count // 2)
    return input_tokens, output_tokens


def _fits(estimate: tuple[int, int], capacity: ModelCapacitySummary) -> bool:
    input_tokens, output_tokens = estimate
    return (
        input_tokens <= capacity.context_token_limit
        and output_tokens <= capacity.max_output_tokens
    )


def _box_hunks(
    hunks: tuple[Hunk, ...], capacity: ModelCapacitySummary
) -> tuple[tuple[Hunk, ...], ...]:
    """First-fit bin packing of whole hunks in their stable order."""

    groups: list[list[Hunk]] = []
    current: list[Hunk] = []
    for hunk in hunks:
        candidate = (*current, hunk)
        if current and not _fits(_estimate_hunks(tuple(candidate)), capacity):
            groups.append(current)
            current = [hunk]
        else:
            current = list(candidate)
    if current:
        groups.append(current)
    return tuple(tuple(group) for group in groups)


def _line_blocks(
    hunk: Hunk, capacity: ModelCapacitySummary
) -> tuple[tuple[Line, ...], ...] | None:
    lines = list(hunk.lines)
    if not lines:
        return None
    block_size = max(
        1,
        min(
            len(lines),
            (capacity.context_token_limit - _FIXED_OVERHEAD_TOKENS)
            * _BYTES_PER_TOKEN
            // 20
            or 1,
        ),
    )
    while block_size >= 1:
        blocks = tuple(
            tuple(lines[start : start + block_size])
            for start in range(0, len(lines), block_size)
        )
        if all(_fits(_estimate_lines(len(block)), capacity) for block in blocks):
            return blocks
        if block_size == 1:
            return None
        block_size -= 1
    return None


def _file_scope(changed_file: ChangedFile) -> CoverageScope:
    return CoverageScope(
        scope_id=_scope_id(changed_file.file_id, "file"),
        file_id=changed_file.file_id,
        scope_kind="file",
        range=None,
        content_digest=None,
        parent_scope_id=None,
        disposition=PlannedDisposition.PLANNED,
    )


def _unreviewable_file_scope(
    changed_file: ChangedFile, *, reason_code: str
) -> CoverageScope:
    return CoverageScope(
        scope_id=_scope_id(changed_file.file_id, "file"),
        file_id=changed_file.file_id,
        scope_kind="file",
        range=None,
        content_digest=None,
        parent_scope_id=None,
        disposition=PlannedDisposition.UNREVIEWABLE,
        reason_code=reason_code,
    )


def _no_review_required_scope(changed_file: ChangedFile) -> CoverageScope:
    return CoverageScope(
        scope_id=_scope_id(changed_file.file_id, "file"),
        file_id=changed_file.file_id,
        scope_kind="file",
        range=None,
        content_digest=None,
        parent_scope_id=None,
        disposition=PlannedDisposition.NO_REVIEW_REQUIRED,
        reason_code="no_text_review_object",
    )


def _hunk_group_scope(
    changed_file: ChangedFile, hunks: tuple[Hunk, ...]
) -> CoverageScope:
    old_numbers = [
        line.old_line_number
        for hunk in hunks
        for line in hunk.lines
        if line.old_line_number is not None
    ]
    new_numbers = [
        line.new_line_number
        for hunk in hunks
        for line in hunk.lines
        if line.new_line_number is not None
    ]
    digest = sha256_digest([hunk.content_digest for hunk in hunks])
    return CoverageScope(
        scope_id=_scope_id(changed_file.file_id, "hunk_group", digest),
        file_id=changed_file.file_id,
        scope_kind="hunk_group",
        range=LineRange(
            old_start=min(old_numbers) if old_numbers else None,
            old_end=max(old_numbers) if old_numbers else None,
            new_start=min(new_numbers) if new_numbers else None,
            new_end=max(new_numbers) if new_numbers else None,
        ),
        content_digest=digest,
        parent_scope_id=None,
        disposition=PlannedDisposition.PLANNED,
    )


def _unreviewable_hunk_scope(
    changed_file: ChangedFile, hunk: Hunk, *, reason_code: str
) -> CoverageScope:
    old_numbers = [
        line.old_line_number for line in hunk.lines if line.old_line_number is not None
    ]
    new_numbers = [
        line.new_line_number for line in hunk.lines if line.new_line_number is not None
    ]
    return CoverageScope(
        scope_id=_scope_id(changed_file.file_id, "hunk_group", hunk.content_digest),
        file_id=changed_file.file_id,
        scope_kind="hunk_group",
        range=LineRange(
            old_start=min(old_numbers) if old_numbers else None,
            old_end=max(old_numbers) if old_numbers else None,
            new_start=min(new_numbers) if new_numbers else None,
            new_end=max(new_numbers) if new_numbers else None,
        ),
        content_digest=hunk.content_digest,
        parent_scope_id=None,
        disposition=PlannedDisposition.UNREVIEWABLE,
        reason_code=reason_code,
    )


def _line_block_scope(
    changed_file: ChangedFile, hunk: Hunk, block: tuple[Line, ...]
) -> CoverageScope:
    old_numbers = [
        line.old_line_number for line in block if line.old_line_number is not None
    ]
    new_numbers = [
        line.new_line_number for line in block if line.new_line_number is not None
    ]
    digest = sha256_digest([line.content_ref.content_digest for line in block])
    return CoverageScope(
        scope_id=_scope_id(changed_file.file_id, "line_block", hunk.hunk_id, digest),
        file_id=changed_file.file_id,
        scope_kind="line_block",
        range=LineRange(
            old_start=min(old_numbers) if old_numbers else None,
            old_end=max(old_numbers) if old_numbers else None,
            new_start=min(new_numbers) if new_numbers else None,
            new_end=max(new_numbers) if new_numbers else None,
        ),
        content_digest=digest,
        parent_scope_id=None,
        disposition=PlannedDisposition.PLANNED,
    )


def _scope_id(*parts: str) -> str:
    return "scope_" + sha256_digest(list(parts))


def _planning_error(code: str) -> StableError:
    return StableError(
        code=code,
        category="planning",
        stage="planning",
        recoverable=False,
        next_actions=("create_new_task",),
    )
