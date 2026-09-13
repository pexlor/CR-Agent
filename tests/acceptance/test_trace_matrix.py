from __future__ import annotations

import pytest

from code_review_agent.domain.trace.models import (
    FactKind,
    TraceCategory,
    TraceEventDraft,
)


def test_trace_matrix_rejects_private_reasoning_payload() -> None:
    with pytest.raises(ValueError):
        TraceEventDraft(
            event_type="model.completed",
            category=TraceCategory.MODEL,
            fact_kind=FactKind.OBSERVATION,
            producer="acceptance",
            summary={"reasoning": "private chain of thought"},
        )
