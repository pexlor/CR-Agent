"""Whitelist-only projection for security audit events."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Final

_EVENT_TYPES: Final = frozenset(
    {
        "artifact_scanned",
        "artifact_redacted",
        "artifact_blocked",
        "scanner_failed",
        "attestation_committed",
        "capability_denied",
    }
)
_REASON_CODES: Final = frozenset(
    {
        "content_safe",
        "sensitive_content_redacted",
        "sensitive_content_blocked",
        "scanner_timeout",
        "scanner_unavailable",
        "scanner_incomplete",
        "unsupported_encoding",
        "finding_range_invalid",
        "security_policy_invalid",
        "capability_denied",
    }
)
_DECISIONS: Final = frozenset({"safe", "redacted", "blocked", "indeterminate"})
_CATEGORIES: Final = frozenset(
    {
        "credential",
        "private_key",
        "certificate_material",
        "connection_string",
        "cloud_secret",
        "personal_data",
        "business_sensitive",
        "unknown_sensitive",
    }
)
_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    event_id: str
    event_type: str
    reason_code: str
    task_id: str | None = None
    policy_id: str | None = None
    decision: str | None = None
    categories: tuple[str, ...] = ()
    finding_count: int = 0

    def __post_init__(self) -> None:
        _validate_identifier(self.event_id, "event_id")
        if self.task_id is not None:
            _validate_identifier(self.task_id, "task_id")
        if self.policy_id is not None:
            _validate_identifier(self.policy_id, "policy_id")
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("event_type is not allowed")
        if self.reason_code not in _REASON_CODES:
            raise ValueError("reason_code is not allowed")
        if self.decision is not None and self.decision not in _DECISIONS:
            raise ValueError("decision is not allowed")
        if any(category not in _CATEGORIES for category in self.categories):
            raise ValueError("category is not allowed")
        if self.finding_count < 0:
            raise ValueError("finding_count must be non-negative")


class SecurityEventLogger:
    """Project an already committed typed event without free-form text."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(self, event: SecurityEvent) -> None:
        payload: dict[str, object] = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "reason_code": event.reason_code,
            "finding_count": event.finding_count,
        }
        if event.task_id is not None:
            payload["task_id"] = event.task_id
        if event.policy_id is not None:
            payload["policy_id"] = event.policy_id
        if event.decision is not None:
            payload["decision"] = event.decision
        if event.categories:
            payload["categories"] = list(event.categories)
        self._logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
