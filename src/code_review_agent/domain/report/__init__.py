"""Immutable report snapshots and format-neutral report models."""

from code_review_agent.domain.report.builder import ReportBuilder
from code_review_agent.domain.report.models import (
    ReportModel,
    ResultSnapshot,
    ResultSnapshotKind,
)

__all__ = ["ReportBuilder", "ReportModel", "ResultSnapshot", "ResultSnapshotKind"]
