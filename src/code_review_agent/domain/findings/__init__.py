"""Deterministic candidate finding validation and consolidation."""

from code_review_agent.domain.findings.models import (
    Confidence,
    CoverageSnapshot,
    FinalFinding,
    FindingProcessingRequest,
    FindingProcessingResult,
    FindingSet,
)
from code_review_agent.domain.findings.processor import FindingProcessor

__all__ = [
    "Confidence",
    "CoverageSnapshot",
    "FinalFinding",
    "FindingProcessingRequest",
    "FindingProcessingResult",
    "FindingProcessor",
    "FindingSet",
]
