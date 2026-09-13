"""Closed registry of value types permitted in persisted checkpoints."""

from __future__ import annotations

from enum import Enum
from typing import Any

from code_review_agent.domain.budget.models import UsageState
from code_review_agent.domain.execution.execution_models import (
    CandidateFinding,
    CandidateLocation,
    CandidateLocationKind,
    CoverageImpact,
    EvidenceReference,
    ModelAttemptOutcomeKind,
    ModelCallAttempt,
    ToolAttempt,
    ToolAttemptState,
    WorkUnitExecutionResult,
    WorkUnitExecutionState,
)
from code_review_agent.domain.execution.models import (
    ModelCallOutcome,
    ModelCallState,
    ModelUsage,
    ProviderState,
    ReservationAction,
    ResponseState,
)
from code_review_agent.domain.planning.models import ToolFailureImpact
from code_review_agent.domain.security.models import (
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    SanitizedArtifactRef,
    SecurityDecision,
)
from code_review_agent.ports.tools import (
    ToolEvidence,
    ToolExecutionResult,
    ToolExecutionState,
)

CheckpointType = type[Enum] | type[Any]

_TRUSTED_TYPES: tuple[CheckpointType, ...] = (
    UsageState,
    CandidateFinding,
    CandidateLocation,
    CandidateLocationKind,
    CoverageImpact,
    EvidenceReference,
    ModelAttemptOutcomeKind,
    ModelCallAttempt,
    ToolAttempt,
    ToolAttemptState,
    WorkUnitExecutionResult,
    WorkUnitExecutionState,
    ModelCallOutcome,
    ModelCallState,
    ModelUsage,
    ProviderState,
    ReservationAction,
    ResponseState,
    ToolFailureImpact,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    SanitizedArtifactRef,
    SecurityDecision,
    ToolEvidence,
    ToolExecutionResult,
    ToolExecutionState,
)


def checkpoint_type_id(value_type: type[Any]) -> str:
    return f"{value_type.__module__}:{value_type.__qualname__}"


TRUSTED_CHECKPOINT_TYPES = {
    checkpoint_type_id(value_type): value_type for value_type in _TRUSTED_TYPES
}


def resolve_checkpoint_type(type_id: str) -> type[Any]:
    try:
        return TRUSTED_CHECKPOINT_TYPES[type_id]
    except KeyError as exc:
        raise ValueError("checkpoint_corrupt") from exc


def require_trusted_checkpoint_type(value_type: type[Any]) -> str:
    type_id = checkpoint_type_id(value_type)
    if TRUSTED_CHECKPOINT_TYPES.get(type_id) is not value_type:
        raise ValueError("checkpoint_value_not_serializable")
    return type_id
