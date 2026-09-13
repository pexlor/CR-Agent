"""Small SQLite store for CLI-visible review state and recovery controls."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from code_review_agent.adapters.sqlite.connection import connect_database
from code_review_agent.application.dto import (
    ReviewProgressView,
    ReviewRunResult,
    StartReviewCommand,
    TraceEventView,
)


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

    def command(
        self, task_id: str
    ) -> tuple[StartReviewCommand, str, str, int]:
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

    def trace(self, task_id: str) -> tuple[TraceEventView, ...]:
        return self.get(task_id).trace

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
            connection.execute("DELETE FROM review_runs WHERE task_id = ?", (task_id,))
            connection.execute(
                "DELETE FROM review_commands WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "INSERT OR REPLACE INTO review_tombstones(task_id, cleaned_at) "
                "VALUES (?, datetime('now'))",
                (task_id,),
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.path)
