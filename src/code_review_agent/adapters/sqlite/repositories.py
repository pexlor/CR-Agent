"""Typed SQLite repositories with no generic SQL surface."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import cast


class TaskRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, task_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone(),
        )

    def insert(self, values: Mapping[str, object]) -> None:
        allowed = {
            "task_id",
            "spec_id",
            "control_state",
            "phase",
            "result_state",
            "delivery_state",
            "version",
        }
        if set(values) != allowed:
            raise ValueError("task payload schema mismatch")
        self._connection.execute(
            "INSERT INTO tasks(task_id, spec_id, control_state, phase, result_state, "
            "delivery_state, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(
                values[key]
                for key in (
                    "task_id",
                    "spec_id",
                    "control_state",
                    "phase",
                    "result_state",
                    "delivery_state",
                    "version",
                )
            ),
        )


class TraceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get_task_events(self, task_id: str) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._connection.execute(
                "SELECT * FROM trace_events WHERE task_id = ? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        )

    def insert(self, values: Mapping[str, object]) -> None:
        allowed = {
            "event_id",
            "task_id",
            "sequence",
            "event_type",
            "event_version",
            "category",
            "fact_kind",
            "summary_json",
            "idempotency_key",
            "schema_digest",
            "created_at",
        }
        if set(values) != allowed:
            raise ValueError("trace payload schema mismatch")
        self._connection.execute(
            "INSERT INTO trace_events(event_id, task_id, sequence, event_type, "
            "event_version, category, fact_kind, summary_json, idempotency_key, "
            "schema_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                values[key]
                for key in (
                    "event_id",
                    "task_id",
                    "sequence",
                    "event_type",
                    "event_version",
                    "category",
                    "fact_kind",
                    "summary_json",
                    "idempotency_key",
                    "schema_digest",
                    "created_at",
                )
            ),
        )


class BudgetRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get_account(self, account_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                "SELECT * FROM budget_accounts WHERE account_id = ?", (account_id,)
            ).fetchone(),
        )

    def insert_ledger_entry(self, values: Mapping[str, object]) -> None:
        allowed = {
            "entry_id",
            "account_id",
            "sequence",
            "entry_type",
            "amount",
            "reservation_amount",
            "created_at",
        }
        if set(values) != allowed:
            raise ValueError("budget payload schema mismatch")
        self._connection.execute(
            "INSERT INTO budget_ledger(entry_id, account_id, sequence, entry_type, "
            "amount, reservation_amount, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(
                values[key]
                for key in (
                    "entry_id",
                    "account_id",
                    "sequence",
                    "entry_type",
                    "amount",
                    "reservation_amount",
                    "created_at",
                )
            ),
        )
