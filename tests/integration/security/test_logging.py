from __future__ import annotations

import json
import logging

import pytest

from code_review_agent.adapters.security.logging import (
    SecurityEvent,
    SecurityEventLogger,
)


def test_security_logger_emits_only_whitelisted_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = SecurityEventLogger(logging.getLogger("security-test"))
    event = SecurityEvent(
        event_id="event-19",
        event_type="artifact_scanned",
        reason_code="sensitive_content_redacted",
        task_id="task-19",
        policy_id="code-review-agent-default",
        decision="redacted",
        categories=("credential",),
        finding_count=1,
    )

    with caplog.at_level(logging.INFO, logger="security-test"):
        logger.emit(event)

    payload = json.loads(caplog.messages[-1])
    assert payload == {
        "categories": ["credential"],
        "decision": "redacted",
        "event_id": "event-19",
        "event_type": "artifact_scanned",
        "finding_count": 1,
        "policy_id": "code-review-agent-default",
        "reason_code": "sensitive_content_redacted",
        "task_id": "task-19",
    }


def test_security_event_rejects_free_form_values() -> None:
    secret = "UniqueMockSecretValue19"

    with pytest.raises(ValueError, match="event_type") as captured:
        SecurityEvent(
            event_id="event-19",
            event_type=secret,
            reason_code="sensitive_content_redacted",
        )

    assert secret not in str(captured.value)
