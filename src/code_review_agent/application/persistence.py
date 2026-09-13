"""Small SQLite store for CLI-visible review state and recovery controls."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from code_review_agent.adapters.sqlite.connection import connect_database
from code_review_agent.application.dto import (
    PersistedTraceArtifactView,
    PersistedTraceEventView,
    ReviewProgressView,
    ReviewRunResult,
    StartReviewCommand,
    TraceEventView,
    TraceSummaryScalar,
)
from code_review_agent.domain.budget.models import (
    BudgetAccount,
    BudgetAccountState,
    BudgetLedgerEntry,
    BudgetReservation,
    LedgerEntryType,
    ReservationState,
    UsageState,
)


@dataclass(frozen=True, slots=True)
class ReviewCheckpoint:
    task_id: str
    input_digest: str
    rules_config_digest: str
    kind: str
    completed_units: tuple[tuple[str, object], ...]
    unknown_units: tuple[tuple[str, object], ...]
    created_at: datetime
    expires_at: datetime


class ReviewStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_runs (
                    task_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_controls (
                    task_id TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0,
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS review_commands (
                    task_id TEXT PRIMARY KEY,
                    diff_text TEXT,
                    diff_file TEXT,
                    output_path TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    source_url TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    budget_tokens INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_tombstones (
                    task_id TEXT PRIMARY KEY,
                    cleaned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_trace_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    security_decision TEXT NOT NULL
                        CHECK(security_decision IN ('safe', 'redacted')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_trace_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    artifact_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE(task_id, sequence),
                    UNIQUE(task_id, idempotency_key),
                    FOREIGN KEY(artifact_id)
                        REFERENCES review_trace_artifacts(artifact_id)
                );
                CREATE TABLE IF NOT EXISTS review_trace_markers (
                    task_id TEXT PRIMARY KEY,
                    first_persisted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_budget_states (
                    task_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_checkpoints (
                    task_id TEXT PRIMARY KEY,
                    input_digest TEXT NOT NULL,
                    rules_config_digest TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_expired_checkpoints (
                    task_id TEXT PRIMARY KEY,
                    expired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_pending_model_calls (
                    model_call_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    work_unit_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    terminal_state TEXT
                );
                """
            )
            for column, definition in (
                ("diff_text", "TEXT"),
                ("output_path", "TEXT"),
                ("subject", "TEXT"),
                ("source_url", "TEXT"),
            ):
                try:
                    connection.execute(
                        f"ALTER TABLE review_commands ADD COLUMN {column} {definition}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
            self._delete_expired_trace(connection, datetime.now(UTC))
            self._mark_expired_checkpoints(connection, datetime.now(UTC))

    def save(self, result: ReviewRunResult) -> None:
        payload = {
            "task_id": result.task_id,
            "session_id": result.session_id,
            "phase": result.phase,
            "result_state": result.result_state,
            "delivery_state": result.delivery_state,
            "report_path": str(result.report_path) if result.report_path else None,
            "report_digest": result.report_digest,
            "limitations": list(result.limitations),
            "trace": [
                {
                    "sequence": event.sequence,
                    "phase": event.phase,
                    "message": event.message,
                }
                for event in result.trace
            ],
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO review_runs(task_id, payload) VALUES (?, ?)",
                (
                    result.task_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )

    def get(self, task_id: str) -> ReviewRunResult:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM review_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise ValueError("task_not_found")
        payload = json.loads(row[0])
        return ReviewRunResult(
            task_id=payload["task_id"],
            session_id=payload["session_id"],
            phase=payload["phase"],
            result_state=payload["result_state"],
            delivery_state=payload["delivery_state"],
            report_path=Path(payload["report_path"])
            if payload["report_path"]
            else None,
            report_digest=payload["report_digest"],
            limitations=tuple(payload["limitations"]),
            trace=tuple(
                TraceEventView(item["sequence"], item["phase"], item["message"])
                for item in payload["trace"]
            ),
        )

    def progress(self, task_id: str) -> ReviewProgressView:
        result = self.get(task_id)
        return ReviewProgressView(
            task_id=result.task_id,
            phase=result.phase,
            result_state=result.result_state,
            delivery_state=result.delivery_state,
            trace=result.trace,
        )

    def save_command(
        self,
        command: StartReviewCommand,
        provider: str,
        model: str,
        budget_tokens: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO review_commands(
                    task_id, diff_text, diff_file, output_path, subject, source_url,
                    provider, model, budget_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.task_id,
                    command.diff_text,
                    str(command.diff_file) if command.diff_file else None,
                    str(command.output_path),
                    command.subject,
                    command.source_url,
                    provider,
                    model,
                    budget_tokens,
                ),
            )

    def command(self, task_id: str) -> tuple[StartReviewCommand, str, str, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT diff_text, diff_file, output_path, subject, source_url, "
                "provider, model, budget_tokens "
                "FROM review_commands WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise ValueError("resume_context_not_found")
        return (
            StartReviewCommand(
                task_id=task_id,
                diff_text=row[0],
                diff_file=Path(row[1]) if row[1] else None,
                output_path=Path(row[2] or f"reports/{task_id}.md"),
                subject=str(row[3] or "Local diff review"),
                source_url=row[4],
                provider=str(row[5]),
                model=str(row[6]),
                budget_tokens=int(row[7]),
            ),
            str(row[5]),
            str(row[6]),
            int(row[7]),
        )

    def save_budget_state(
        self,
        account: BudgetAccount,
        entries: tuple[BudgetLedgerEntry, ...],
        reservations: tuple[BudgetReservation, ...],
    ) -> None:
        payload = {
            "account": {
                "account_id": account.account_id,
                "task_id": account.task_id,
                "unit": account.unit,
                "state": account.state.value,
                "revision": account.revision,
                "last_sequence": account.last_sequence,
                "provider_capability_baseline_ref": (
                    account.provider_capability_baseline_ref
                ),
                "active_freeze_id": account.active_freeze_id,
                "pending_revalidation": account.pending_revalidation,
            },
            "entries": [
                {
                    "entry_id": entry.entry_id,
                    "entry_type": entry.entry_type.value,
                    "amount": entry.amount,
                    "sequence": entry.sequence,
                    "reservation_amount": entry.reservation_amount,
                    "usage_state": (
                        entry.usage_state.value if entry.usage_state else None
                    ),
                    "created_at": entry.created_at.isoformat(),
                }
                for entry in entries
            ],
            "reservations": [
                {
                    "reservation_id": reservation.reservation_id,
                    "account_id": reservation.account_id,
                    "task_id": reservation.task_id,
                    "model_call_id": reservation.model_call_id,
                    "work_unit_id": reservation.work_unit_id,
                    "input_bound": reservation.input_bound,
                    "output_max": reservation.output_max,
                    "amount": reservation.amount,
                    "prepared_request_digest": reservation.prepared_request_digest,
                    "capability_ref": reservation.capability_ref,
                    "state": reservation.state.value,
                    "usage_state": reservation.usage_state.value,
                    "actual_usage": reservation.actual_usage,
                }
                for reservation in reservations
            ],
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO review_budget_states(task_id, payload) "
                "VALUES (?, ?)",
                (
                    account.task_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )

    def load_budget_state(
        self, task_id: str
    ) -> tuple[
        BudgetAccount, tuple[BudgetLedgerEntry, ...], tuple[BudgetReservation, ...]
    ] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM review_budget_states WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        account_data = payload["account"]
        account = BudgetAccount(
            account_id=str(account_data["account_id"]),
            task_id=str(account_data["task_id"]),
            unit=str(account_data["unit"]),
            state=BudgetAccountState(str(account_data["state"])),
            revision=int(account_data["revision"]),
            last_sequence=int(account_data["last_sequence"]),
            provider_capability_baseline_ref=str(
                account_data["provider_capability_baseline_ref"]
            ),
            active_freeze_id=account_data["active_freeze_id"],
            pending_revalidation=bool(account_data["pending_revalidation"]),
        )
        entries = tuple(
            BudgetLedgerEntry(
                entry_id=str(item["entry_id"]),
                entry_type=LedgerEntryType(str(item["entry_type"])),
                amount=int(item["amount"]),
                sequence=int(item["sequence"]),
                reservation_amount=int(item["reservation_amount"]),
                usage_state=(
                    UsageState(str(item["usage_state"]))
                    if item["usage_state"] is not None
                    else None
                ),
                created_at=datetime.fromisoformat(str(item["created_at"])),
            )
            for item in payload["entries"]
        )
        reservations = tuple(
            BudgetReservation(
                reservation_id=str(item["reservation_id"]),
                account_id=str(item["account_id"]),
                task_id=str(item["task_id"]),
                model_call_id=str(item["model_call_id"]),
                work_unit_id=str(item["work_unit_id"]),
                input_bound=int(item["input_bound"]),
                output_max=int(item["output_max"]),
                amount=int(item["amount"]),
                prepared_request_digest=str(item["prepared_request_digest"]),
                capability_ref=str(item["capability_ref"]),
                state=ReservationState(str(item["state"])),
                usage_state=UsageState(str(item["usage_state"])),
                actual_usage=(
                    int(item["actual_usage"])
                    if item["actual_usage"] is not None
                    else None
                ),
            )
            for item in payload["reservations"]
        )
        return account, entries, reservations

    def save_checkpoint(
        self,
        *,
        task_id: str,
        input_digest: str,
        rules_config_digest: str,
        kind: str,
        work_unit_id: str | None = None,
        execution: object | None = None,
        unknown_execution: object | None = None,
        expires_at: datetime | None = None,
    ) -> ReviewCheckpoint:
        if not all((task_id, input_digest, rules_config_digest, kind)):
            raise ValueError("checkpoint_fields_required")
        now = datetime.now(UTC)
        expiry = expires_at or now + timedelta(days=7)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT input_digest, rules_config_digest, payload, created_at "
                "FROM review_checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            completed: dict[str, object] = {}
            unknown: dict[str, object] = {}
            created_at = now
            if row is not None:
                if str(row[0]) != input_digest or str(row[1]) != rules_config_digest:
                    raise ValueError("checkpoint_binding_mismatch")
                completed, unknown = self._decode_checkpoint_payload(str(row[2]))
                created_at = datetime.fromisoformat(str(row[3]))
            if work_unit_id is not None:
                if execution is None and unknown_execution is None:
                    raise ValueError("checkpoint_execution_required")
                if execution is not None:
                    completed[work_unit_id] = execution
            if unknown_execution is not None:
                if work_unit_id is None:
                    raise ValueError("checkpoint_work_unit_required")
                unknown[work_unit_id] = unknown_execution
            payload = json.dumps(
                {
                    "completed": {
                        unit_id: self._encode_checkpoint_value(value)
                        for unit_id, value in sorted(completed.items())
                    },
                    "unknown": {
                        unit_id: self._encode_checkpoint_value(value)
                        for unit_id, value in sorted(unknown.items())
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(payload.encode()).hexdigest()
            connection.execute(
                "INSERT OR REPLACE INTO review_checkpoints "
                "(task_id, input_digest, rules_config_digest, kind, payload, "
                "payload_digest, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    input_digest,
                    rules_config_digest,
                    kind,
                    payload,
                    digest,
                    created_at.isoformat(),
                    expiry.astimezone(UTC).isoformat(),
                ),
            )
        return ReviewCheckpoint(
            task_id=task_id,
            input_digest=input_digest,
            rules_config_digest=rules_config_digest,
            kind=kind,
            completed_units=tuple(sorted(completed.items())),
            unknown_units=tuple(sorted(unknown.items())),
            created_at=created_at,
            expires_at=expiry,
        )

    def load_checkpoint(
        self, task_id: str, *, input_digest: str, rules_config_digest: str
    ) -> ReviewCheckpoint:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT input_digest, rules_config_digest, kind, payload, "
                "payload_digest, created_at, expires_at FROM review_checkpoints "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            with self._connect() as connection:
                expired = connection.execute(
                    "SELECT 1 FROM review_expired_checkpoints WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
            if expired is not None:
                raise ValueError("checkpoint_expired")
            raise ValueError("checkpoint_not_found")
        if str(row[0]) != input_digest or str(row[1]) != rules_config_digest:
            raise ValueError("checkpoint_binding_mismatch")
        payload = str(row[3])
        if hashlib.sha256(payload.encode()).hexdigest() != str(row[4]):
            raise ValueError("checkpoint_corrupt")
        try:
            completed, unknown = self._decode_checkpoint_payload(payload)
            created_at = datetime.fromisoformat(str(row[5]))
            expires_at = datetime.fromisoformat(str(row[6]))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("checkpoint_corrupt") from exc
        if expires_at <= datetime.now(UTC):
            raise ValueError("checkpoint_expired")
        return ReviewCheckpoint(
            task_id=task_id,
            input_digest=input_digest,
            rules_config_digest=rules_config_digest,
            kind=str(row[2]),
            completed_units=tuple(sorted(completed.items())),
            unknown_units=tuple(sorted(unknown.items())),
            created_at=created_at,
            expires_at=expires_at,
        )

    def append_trace_event(
        self,
        *,
        task_id: str,
        event_id: str,
        event_type: str,
        category: str,
        summary: Mapping[str, object],
        idempotency_key: str,
        artifact: PersistedTraceArtifactView | None = None,
        expires_at: datetime | None = None,
    ) -> PersistedTraceEventView:
        normalized = self._normalize_summary(summary)
        summary_json = json.dumps(
            dict(normalized), sort_keys=True, separators=(",", ":")
        )
        if artifact is not None:
            digest = hashlib.sha256(artifact.content.encode()).hexdigest()
            if digest != artifact.content_digest:
                raise ValueError("trace_artifact_digest_mismatch")
            if artifact.security_decision not in {"safe", "redacted"}:
                raise ValueError("trace_artifact_security_decision_invalid")
        created = datetime.now(UTC)
        expiry = expires_at or created + timedelta(days=7)
        created_text = created.isoformat()
        expiry_text = expiry.astimezone(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_id FROM review_trace_events "
                "WHERE task_id = ? AND idempotency_key = ?",
                (task_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                persisted = self._trace_event(connection, str(existing[0]))
                candidate_artifact = artifact
                if candidate_artifact is not None:
                    candidate_artifact = PersistedTraceArtifactView(
                        artifact_id=candidate_artifact.artifact_id,
                        purpose=candidate_artifact.purpose,
                        content=candidate_artifact.content,
                        content_digest=candidate_artifact.content_digest,
                        security_decision=candidate_artifact.security_decision,
                        created_at=(
                            persisted.artifact.created_at if persisted.artifact else ""
                        ),
                        expires_at=(
                            persisted.artifact.expires_at if persisted.artifact else ""
                        ),
                    )
                if (
                    persisted.event_id != event_id
                    or persisted.event_type != event_type
                    or persisted.category != category
                    or persisted.summary != normalized
                    or persisted.artifact != candidate_artifact
                ):
                    raise ValueError("trace_idempotency_conflict")
                return persisted
            connection.execute(
                "INSERT OR IGNORE INTO review_trace_markers"
                "(task_id, first_persisted_at) VALUES (?, ?)",
                (task_id, created_text),
            )
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM review_trace_events "
                    "WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
            )
            artifact_id: str | None = None
            if artifact is not None:
                artifact_id = artifact.artifact_id
                connection.execute(
                    "INSERT INTO review_trace_artifacts "
                    "(artifact_id, task_id, purpose, content, content_digest, "
                    "security_decision, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact_id,
                        task_id,
                        artifact.purpose,
                        artifact.content,
                        artifact.content_digest,
                        artifact.security_decision,
                        created_text,
                        expiry_text,
                    ),
                )
            connection.execute(
                "INSERT INTO review_trace_events "
                "(event_id, task_id, sequence, event_type, category, summary_json, "
                "artifact_id, idempotency_key, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    task_id,
                    sequence,
                    event_type,
                    category,
                    summary_json,
                    artifact_id,
                    idempotency_key,
                    created_text,
                    expiry_text,
                ),
            )
            return self._trace_event(connection, event_id)

    def trace(self, task_id: str) -> tuple[PersistedTraceEventView, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id FROM review_trace_events WHERE task_id = ? "
                "ORDER BY sequence",
                (task_id,),
            ).fetchall()
            if not rows:
                raise ValueError("trace_not_found")
            return tuple(self._trace_event(connection, str(row[0])) for row in rows)

    def cleanup_trace(self, task_id: str) -> None:
        with self._connect() as connection:
            self._delete_task_trace(connection, task_id)

    def has_trace_history(self, task_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM review_trace_markers WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row is not None

    def request_pause(self, task_id: str, reason: str) -> None:
        if not reason:
            raise ValueError("pause_reason_required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO review_controls(task_id, paused, reason)
                VALUES (?, 1, ?)
                ON CONFLICT(task_id) DO UPDATE SET paused = 1, reason = excluded.reason
                """,
                (task_id, reason),
            )

    def clear_pause(self, task_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE review_controls SET paused = 0, reason = NULL "
                "WHERE task_id = ?",
                (task_id,),
            )

    def is_paused(self, task_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT paused FROM review_controls WHERE task_id = ?", (task_id,)
            ).fetchone()
        return bool(row and row[0])

    def terminate(self, task_id: str) -> ReviewRunResult:
        result = self.get(task_id)
        updated = ReviewRunResult(
            task_id=result.task_id,
            session_id=result.session_id,
            phase="terminated",
            result_state=result.result_state,
            delivery_state=result.delivery_state,
            report_path=result.report_path,
            report_digest=result.report_digest,
            limitations=tuple((*result.limitations, "terminated")),
            trace=result.trace,
        )
        self.save(updated)
        return updated

    def cleanup(self, task_id: str) -> None:
        self.get(task_id)
        with self._connect() as connection:
            self._delete_task_trace(connection, task_id)
            connection.execute("DELETE FROM review_runs WHERE task_id = ?", (task_id,))
            connection.execute(
                "DELETE FROM review_commands WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "DELETE FROM review_budget_states WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "DELETE FROM review_checkpoints WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "DELETE FROM review_expired_checkpoints WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "DELETE FROM review_pending_model_calls WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "INSERT OR REPLACE INTO review_tombstones(task_id, cleaned_at) "
                "VALUES (?, datetime('now'))",
                (task_id,),
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.path)

    @staticmethod
    def _normalize_summary(
        summary: Mapping[str, object],
    ) -> Mapping[str, TraceSummaryScalar]:
        forbidden = {"reasoning", "chain_of_thought", "content", "raw", "response"}
        normalized: dict[str, TraceSummaryScalar] = {}
        for key, value in summary.items():
            if not isinstance(key, str) or key.lower() in forbidden:
                raise ValueError("trace_summary_invalid")
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("trace_summary_invalid")
            normalized[key] = value
        return MappingProxyType(dict(sorted(normalized.items())))

    @staticmethod
    def _delete_task_trace(connection: sqlite3.Connection, task_id: str) -> None:
        connection.execute(
            "DELETE FROM review_trace_events WHERE task_id = ?", (task_id,)
        )
        connection.execute(
            "DELETE FROM review_trace_artifacts WHERE task_id = ?", (task_id,)
        )

    @staticmethod
    def _delete_expired_trace(connection: sqlite3.Connection, now: datetime) -> None:
        timestamp = now.isoformat()
        connection.execute(
            "DELETE FROM review_trace_events WHERE expires_at <= ?", (timestamp,)
        )
        connection.execute(
            "DELETE FROM review_trace_artifacts WHERE expires_at <= ?", (timestamp,)
        )

    @staticmethod
    def _mark_expired_checkpoints(
        connection: sqlite3.Connection, now: datetime
    ) -> None:
        timestamp = now.isoformat()
        connection.execute(
            "INSERT OR REPLACE INTO review_expired_checkpoints(task_id, expired_at) "
            "SELECT task_id, expires_at FROM review_checkpoints WHERE expires_at <= ?",
            (timestamp,),
        )
        connection.execute(
            "DELETE FROM review_checkpoints WHERE expires_at <= ?", (timestamp,)
        )

    @classmethod
    def _encode_checkpoint_value(cls, value: object) -> object:
        # StrEnum values are also str instances; the Enum branch must run first
        # so that JSON round-tripping preserves the enum type instead of
        # collapsing it to a plain string.
        if isinstance(value, Enum):
            return {
                "__enum__": f"{type(value).__module__}:{type(value).__qualname__}",
                "value": value.value,
            }
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, tuple):
            return {"__tuple__": [cls._encode_checkpoint_value(item) for item in value]}
        if isinstance(value, list):
            return [cls._encode_checkpoint_value(item) for item in value]
        if isinstance(value, Mapping):
            return {
                str(key): cls._encode_checkpoint_value(item)
                for key, item in value.items()
            }
        if is_dataclass(value) and not isinstance(value, type):
            safe_fields = {
                field.name: cls._encode_checkpoint_value(getattr(value, field.name))
                for field in fields(value)
                if field.init and field.name not in {"raw", "content", "diff_text"}
            }
            return {
                "__dataclass__": f"{type(value).__module__}:{type(value).__qualname__}",
                "fields": safe_fields,
            }
        raise ValueError("checkpoint_value_not_serializable")

    @classmethod
    def _decode_checkpoint_value(cls, value: object) -> object:
        if not isinstance(value, (dict, list)):
            return value
        if isinstance(value, list):
            return [cls._decode_checkpoint_value(item) for item in value]
        if "__tuple__" in value:
            items = value["__tuple__"]
            if not isinstance(items, list):
                raise ValueError("checkpoint_corrupt")
            return tuple(cls._decode_checkpoint_value(item) for item in items)
        type_ref = value.get("__enum__") or value.get("__dataclass__")
        if type_ref is not None:
            module_name, separator, qualname = str(type_ref).partition(":")
            if not separator or (
                not module_name.startswith(("code_review_agent.", "tests."))
                and not module_name.startswith("test_")
            ):
                raise ValueError("checkpoint_corrupt")
            target: object = importlib.import_module(module_name)
            for part in qualname.split("."):
                target = getattr(target, part)
            if "__enum__" in value:
                return target(value["value"])  # type: ignore[operator]
            raw_fields = value.get("fields")
            if not isinstance(raw_fields, dict):
                raise ValueError("checkpoint_corrupt")
            decoded = {
                str(key): cls._decode_checkpoint_value(item)
                for key, item in raw_fields.items()
            }
            return target(**decoded)  # type: ignore[operator]
        return {
            str(key): cls._decode_checkpoint_value(item) for key, item in value.items()
        }

    @classmethod
    def _decode_checkpoint_payload(
        cls, payload: str
    ) -> tuple[dict[str, object], dict[str, object]]:
        loaded = json.loads(payload)
        if not isinstance(loaded, dict):
            raise ValueError("checkpoint_corrupt")
        # Read checkpoints created before unknown state was separated.
        completed_raw = loaded.get("completed", loaded)
        unknown_raw = loaded.get("unknown", {})
        if not isinstance(completed_raw, dict) or not isinstance(unknown_raw, dict):
            raise ValueError("checkpoint_corrupt")
        completed = {
            str(key): cls._decode_checkpoint_value(value)
            for key, value in completed_raw.items()
        }
        unknown = {
            str(key): cls._decode_checkpoint_value(value)
            for key, value in unknown_raw.items()
        }
        return completed, unknown

    def begin_model_call(
        self,
        *,
        task_id: str,
        model_call_id: str,
        work_unit_id: str,
        execution_id: str,
        reservation_id: str,
        request_digest: str,
    ) -> None:
        self.append_trace_event(
            task_id=task_id,
            event_id=f"{task_id}:{model_call_id}:started",
            event_type="model.call_started",
            category="model",
            summary={
                "model_call_id": model_call_id,
                "work_unit_id": work_unit_id,
                "reservation_id": reservation_id,
            },
            idempotency_key=f"{model_call_id}-started",
        )
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO review_pending_model_calls(model_call_id, task_id, "
                "work_unit_id, execution_id, reservation_id, request_digest, "
                "started_at, terminal_state) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    model_call_id,
                    task_id,
                    work_unit_id,
                    execution_id,
                    reservation_id,
                    request_digest,
                    now,
                ),
            )

    def finish_model_call(self, model_call_id: str, *, terminal_state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE review_pending_model_calls SET terminal_state = ? "
                "WHERE model_call_id = ?",
                (terminal_state, model_call_id),
            )

    def pending_model_calls(
        self, task_id: str
    ) -> tuple[tuple[str, str, str, str, str], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT model_call_id, work_unit_id, execution_id, reservation_id, "
                "request_digest FROM review_pending_model_calls "
                "WHERE task_id = ? AND terminal_state IS NULL ORDER BY started_at",
                (task_id,),
            ).fetchall()
        return tuple(
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]))
            for row in rows
        )

    def recover_pending_model_calls(self, task_id: str) -> tuple[str, ...]:
        pending = self.pending_model_calls(task_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for model_call_id, _, _, _, _ in pending:
                connection.execute(
                    "UPDATE review_pending_model_calls SET terminal_state = 'unknown' "
                    "WHERE model_call_id = ? AND terminal_state IS NULL",
                    (model_call_id,),
                )
        return tuple(item[3] for item in pending)

    def has_unknown_execution(self, task_id: str) -> bool:
        with self._connect() as connection:
            pending_or_unknown = connection.execute(
                "SELECT 1 FROM review_pending_model_calls WHERE task_id = ? "
                "AND (terminal_state IS NULL OR terminal_state = 'unknown') LIMIT 1",
                (task_id,),
            ).fetchone()
        if pending_or_unknown is not None:
            return True
        try:
            command, _, _, _ = self.command(task_id)
        except ValueError:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM review_checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return False
        try:
            _, unknown = self._decode_checkpoint_payload(str(row[0]))
        except ValueError:
            return False
        return bool(unknown) or bool(
            command.task_id and self.pending_model_calls(task_id)
        )

    @staticmethod
    def _trace_event(
        connection: sqlite3.Connection, event_id: str
    ) -> PersistedTraceEventView:
        row = connection.execute(
            "SELECT e.event_id, e.task_id, e.sequence, e.event_type, e.category, "
            "e.summary_json, e.idempotency_key, e.created_at, e.expires_at, "
            "a.artifact_id, a.purpose, a.content, a.content_digest, "
            "a.security_decision, a.created_at, a.expires_at "
            "FROM review_trace_events e LEFT JOIN review_trace_artifacts a "
            "ON a.artifact_id = e.artifact_id WHERE e.event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise ValueError("trace_not_found")
        loaded = json.loads(str(row[5]))
        if not isinstance(loaded, dict):
            raise ValueError("trace_summary_invalid")
        summary = ReviewStateStore._normalize_summary(
            {str(key): value for key, value in loaded.items()}
        )
        artifact = None
        if row[9] is not None:
            artifact = PersistedTraceArtifactView(
                artifact_id=str(row[9]),
                purpose=str(row[10]),
                content=str(row[11]),
                content_digest=str(row[12]),
                security_decision=str(row[13]),
                created_at=str(row[14]),
                expires_at=str(row[15]),
            )
        return PersistedTraceEventView(
            event_id=str(row[0]),
            task_id=str(row[1]),
            sequence=int(row[2]),
            event_type=str(row[3]),
            category=str(row[4]),
            summary=summary,
            idempotency_key=str(row[6]),
            created_at=str(row[7]),
            expires_at=str(row[8]),
            artifact=artifact,
        )
