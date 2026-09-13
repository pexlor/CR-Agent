"""Human and JSON presentation of application DTOs."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {
            key: _json_value(item) for key, item in asdict(cast(Any, value)).items()
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
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
    if hasattr(data, "report_path"):
        return (
            f"task_id={data.task_id} result={data.result_state} "
            f"delivery={data.delivery_state} report={data.report_path}"
        )
    if hasattr(data, "task_id"):
        return (
            f"task_id={data.task_id} phase={data.phase} "
            f"result={data.result_state} delivery={data.delivery_state}"
        )
    return str(data)


def human_trace(trace_id: str, events: tuple[Any, ...]) -> str:
    return "\n".join(f"{trace_id} [{event.phase}] {event.message}" for event in events)
