"""Immutable review planning contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.input.models import ArtifactSliceRef
from code_review_agent.domain.security.models import SecurityDecision
from code_review_agent.ports.tools import FixedToolReference, ToolLimits

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PlannedDisposition(StrEnum):
    PLANNED = "planned"
    UNREVIEWABLE = "unreviewable"
    NO_REVIEW_REQUIRED = "no_review_required"


class WorkUnitKind(StrEnum):
    FILE = "file"
    HUNK_GROUP = "hunk_group"
    LINE_BLOCK = "line_block"


class ToolFailureImpact(StrEnum):
    EVIDENCE_DEGRADED = "evidence_degraded"
    COVERAGE_DEGRADED = "coverage_degraded"


@dataclass(frozen=True, slots=True)
class PlanningStrategy:
    """Fixed, versioned splitting policy for one planning run."""

    strategy_id: str
    strategy_version: str

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.strategy_version:
            raise ValueError("planning strategy identity is required")


@dataclass(frozen=True, slots=True)
class ModelCapacitySummary:
    """The subset of model capabilities the planner is allowed to see."""

    provider_id: str
    provider_version: str
    model_id: str
    context_token_limit: int
    max_output_tokens: int
    token_counting_version: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.provider_id,
                self.provider_version,
                self.model_id,
                self.token_counting_version,
            )
        ):
            raise ValueError("model capacity identity is required")
        if (
            type(self.context_token_limit) is not int
            or type(self.max_output_tokens) is not int
            or self.context_token_limit <= 0
            or self.max_output_tokens <= 0
        ):
            raise ValueError("model capacity limits must be positive integers")

    def digest_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "model_id": self.model_id,
            "context_token_limit": self.context_token_limit,
            "max_output_tokens": self.max_output_tokens,
            "token_counting_version": self.token_counting_version,
        }


@dataclass(frozen=True, slots=True)
class LineRange:
    old_start: int | None
    old_end: int | None
    new_start: int | None
    new_end: int | None

    def __post_init__(self) -> None:
        if (self.old_start is None) != (self.old_end is None):
            raise ValueError("old range bounds must both be set or both be empty")
        if (self.new_start is None) != (self.new_end is None):
            raise ValueError("new range bounds must both be set or both be empty")
        if (
            self.old_start is not None
            and self.old_end is not None
            and (self.old_start < 0 or self.old_end < self.old_start)
        ):
            raise ValueError("old range must be valid")
        if (
            self.new_start is not None
            and self.new_end is not None
            and (self.new_start < 0 or self.new_end < self.new_start)
        ):
            raise ValueError("new range must be valid")
        if self.old_start is None and self.new_start is None:
            raise ValueError("line range must cover at least one side")


@dataclass(frozen=True, slots=True)
class CoverageScope:
    """The smallest coverage unit shared by planning and result reporting."""

    scope_id: str
    file_id: str
    scope_kind: str
    range: LineRange | None
    content_digest: str | None
    parent_scope_id: str | None
    disposition: PlannedDisposition
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.scope_id or not self.file_id or not self.scope_kind:
            raise ValueError("coverage scope identity is required")
        if self.content_digest is not None and not _SHA256.fullmatch(
            self.content_digest
        ):
            raise ValueError("coverage scope digest must be lowercase SHA-256")
        if (
            self.disposition is not PlannedDisposition.PLANNED
            and self.reason_code is None
        ):
            raise ValueError("non-planned scopes must state a reason code")
        if (
            self.disposition is PlannedDisposition.PLANNED
            and self.reason_code is not None
        ):
            raise ValueError("planned scopes must not carry a skip reason")


@dataclass(frozen=True, slots=True)
class ToolSelection:
    """A fixed, catalog-backed tool choice for one work unit."""

    fixed_reference: FixedToolReference
    applicable_rule_ids: tuple[str, ...]
    applicability_reason: str
    planned_limits: ToolLimits
    failure_impact: ToolFailureImpact
    order: int

    def __post_init__(self) -> None:
        if not self.applicability_reason:
            raise ValueError("tool selection reason is required")
        if len(set(self.applicable_rule_ids)) != len(self.applicable_rule_ids):
            raise ValueError("tool selection rule ids must be unique")
        if self.order < 0:
            raise ValueError("tool selection order must be non-negative")

    def digest_payload(self) -> dict[str, object]:
        return {
            "tool_id": self.fixed_reference.tool_id,
            "version": self.fixed_reference.version,
            "declaration_digest": self.fixed_reference.declaration_digest,
            "applicable_rule_ids": list(self.applicable_rule_ids),
            "applicability_reason": self.applicability_reason,
            "planned_limits": self.planned_limits.digest_payload(),
            "failure_impact": self.failure_impact.value,
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """A declared, non-executed request for controlled surrounding context."""

    purpose: str
    allowed_path_categories: tuple[str, ...]
    max_bytes_per_path: int
    max_total_bytes: int

    def __post_init__(self) -> None:
        if not self.purpose:
            raise ValueError("context request purpose is required")
        if any(not category for category in self.allowed_path_categories):
            raise ValueError("context request path categories must be non-empty")
        if self.max_bytes_per_path <= 0 or self.max_total_bytes <= 0:
            raise ValueError("context request byte limits must be positive")
        if self.max_bytes_per_path > self.max_total_bytes:
            raise ValueError("per-path limit cannot exceed the total limit")

    def digest_payload(self) -> dict[str, object]:
        return {
            "purpose": self.purpose,
            "allowed_path_categories": list(self.allowed_path_categories),
            "max_bytes_per_path": self.max_bytes_per_path,
            "max_total_bytes": self.max_total_bytes,
        }


@dataclass(frozen=True, slots=True)
class CapacityEstimate:
    """A conservative, deterministic estimate of context usage."""

    estimator_version: str
    estimated_input_tokens: int
    estimated_output_tokens: int

    def __post_init__(self) -> None:
        if not self.estimator_version:
            raise ValueError("capacity estimator version is required")
        if self.estimated_input_tokens < 0 or self.estimated_output_tokens < 0:
            raise ValueError("capacity estimates must be non-negative")

    def digest_payload(self) -> dict[str, object]:
        return {
            "estimator_version": self.estimator_version,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class WorkUnit:
    """One reviewable, independently executable and persistable unit."""

    work_unit_id: str
    plan_id: str
    kind: WorkUnitKind
    file_id: str
    scope_ids: tuple[str, ...]
    range: LineRange
    content_refs: tuple[ArtifactSliceRef, ...]
    context_request: ContextRequest | None
    tools: tuple[ToolSelection, ...]
    capacity_estimate: CapacityEstimate
    strategy: PlanningStrategy
    model_capacity: ModelCapacitySummary
    execution_rank: int
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.work_unit_id or not self.plan_id or not self.file_id:
            raise ValueError("work unit identity is required")
        if not self.scope_ids or len(set(self.scope_ids)) != len(self.scope_ids):
            raise ValueError("work unit scopes must be unique and non-empty")
        if not self.content_refs:
            raise ValueError("work unit must reference at least one content slice")
        if any(
            ref.artifact.decision is SecurityDecision.BLOCKED
            for ref in self.content_refs
        ):
            raise ValueError("blocked content cannot enter a work unit")
        if self.execution_rank < 0:
            raise ValueError("execution rank must be non-negative")
        orders = tuple(tool.order for tool in self.tools)
        if len(set(orders)) != len(orders):
            raise ValueError("tool selections must have unique order values")
        object.__setattr__(
            self,
            "fingerprint",
            sha256_digest(
                {
                    "plan_id": self.plan_id,
                    "kind": self.kind.value,
                    "file_id": self.file_id,
                    "scope_ids": list(self.scope_ids),
                    "content_refs": [
                        [
                            ref.artifact.sanitized_digest,
                            ref.start,
                            ref.end,
                            ref.content_digest,
                        ]
                        for ref in self.content_refs
                    ],
                    "context_request": (
                        self.context_request.digest_payload()
                        if self.context_request is not None
                        else None
                    ),
                    "tools": [tool.digest_payload() for tool in self.tools],
                    "capacity_estimate": self.capacity_estimate.digest_payload(),
                    "strategy": [
                        self.strategy.strategy_id,
                        self.strategy.strategy_version,
                    ],
                    "model_capacity": self.model_capacity.digest_payload(),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """The atomic, frozen output of the review planner."""

    plan_id: str
    plan_version: int
    task_id: str
    input_binding_id: str
    change_set_id: str
    change_set_digest: str
    strategy: PlanningStrategy
    model_capacity: ModelCapacitySummary
    tool_catalog_digest: str
    coverage_scopes: tuple[CoverageScope, ...]
    work_units: tuple[WorkUnit, ...]
    plan_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.plan_id,
                self.task_id,
                self.input_binding_id,
                self.change_set_id,
                self.tool_catalog_digest,
            )
        ):
            raise ValueError("review plan identity is required")
        if self.plan_version != 1:
            raise ValueError("MVP review plans must be version 1")
        if not _SHA256.fullmatch(self.change_set_digest):
            raise ValueError("change set digest must be lowercase SHA-256")

        scope_ids = tuple(scope.scope_id for scope in self.coverage_scopes)
        if len(set(scope_ids)) != len(scope_ids):
            raise ValueError("coverage scope identities must be unique")

        planned_ids = {
            scope.scope_id
            for scope in self.coverage_scopes
            if scope.disposition is PlannedDisposition.PLANNED
        }
        covered_by_units: list[str] = []
        for unit in self.work_units:
            covered_by_units.extend(unit.scope_ids)
        if len(set(covered_by_units)) != len(covered_by_units):
            raise ValueError("planned scopes must be covered by exactly one work unit")
        if set(covered_by_units) != planned_ids:
            raise ValueError(
                "every planned scope must be covered exactly once, and only "
                "planned scopes may be covered"
            )

        work_unit_ids = tuple(unit.work_unit_id for unit in self.work_units)
        if len(set(work_unit_ids)) != len(work_unit_ids):
            raise ValueError("work unit identities must be unique")
        for unit in self.work_units:
            if unit.plan_id != self.plan_id:
                raise ValueError("work unit does not belong to this plan")

        object.__setattr__(
            self,
            "plan_fingerprint",
            sha256_digest(
                {
                    "task_id": self.task_id,
                    "input_binding_id": self.input_binding_id,
                    "change_set_id": self.change_set_id,
                    "change_set_digest": self.change_set_digest,
                    "strategy": [
                        self.strategy.strategy_id,
                        self.strategy.strategy_version,
                    ],
                    "model_capacity": self.model_capacity.digest_payload(),
                    "tool_catalog_digest": self.tool_catalog_digest,
                    "coverage_scopes": [
                        {
                            "scope_id": scope.scope_id,
                            "file_id": scope.file_id,
                            "scope_kind": scope.scope_kind,
                            "disposition": scope.disposition.value,
                            "reason_code": scope.reason_code,
                            "content_digest": scope.content_digest,
                        }
                        for scope in self.coverage_scopes
                    ],
                    "work_units": [
                        {
                            "work_unit_id": unit.work_unit_id,
                            "kind": unit.kind.value,
                            "file_id": unit.file_id,
                            "scope_ids": list(unit.scope_ids),
                            "fingerprint": unit.fingerprint,
                        }
                        for unit in self.work_units
                    ],
                }
            ),
        )
