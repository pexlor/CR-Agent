"""Human and JSON presentation of application DTOs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, cast

from code_review_agent.application.dto import (
    PersistedTraceEventView,
    TraceEventView,
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(cast(Any, value))
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {str(key): _json_value(item) for key, item in vars(value).items()}
    return value


def json_envelope(command: str, request_id: str, data: Any) -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "command": command,
            "request_id": request_id,
            "ok": True,
            "data": _json_value(data),
            "error": None,
            "warnings": [],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def human_result(data: Any) -> str:
    if hasattr(data, "publication_id"):
        return (
            f"task_id={data.task_id} publication={data.state} "
            f"published={data.published} skipped={data.skipped} "
            f"failed={data.failed} unknown={data.unknown}"
        )
    if hasattr(data, "report_path"):
        publication = ""
        if getattr(data, "publication", None) is not None:
            publication = f" publication={data.publication.state}"
        return (
            f"task_id={data.task_id} result={data.result_state} "
            f"delivery={data.delivery_state} report={data.report_path}{publication}"
        )
    if hasattr(data, "task_id"):
        publication = ""
        if getattr(data, "publication_state", None) is not None:
            publication = f" publication={data.publication_state}"
        return (
            f"task_id={data.task_id} phase={data.phase} "
            f"result={data.result_state} delivery={data.delivery_state}{publication}"
        )
    return str(data)


def human_trace(trace_id: str, events: tuple[object, ...]) -> str:
    lines: list[str] = []
    for event in events:
        if isinstance(event, PersistedTraceEventView):
            summary = json.dumps(
                dict(event.summary), sort_keys=True, separators=(",", ":")
            )
            artifact = ""
            if event.artifact is not None:
                artifact = (
                    f" artifact={event.artifact.artifact_id}:"
                    f"{event.artifact.purpose}:{event.artifact.security_decision}"
                )
            lines.append(
                f"{trace_id} [{event.category}] {event.event_type} "
                f"summary={summary}{artifact}"
            )
        elif isinstance(event, TraceEventView):
            lines.append(f"{trace_id} [{event.phase}] {event.message}")
        else:
            raise TypeError("trace_event_invalid")
    return "\n".join(lines)
