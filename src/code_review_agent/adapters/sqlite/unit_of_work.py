"""Single SQLite transaction boundary for typed persistence operations."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from code_review_agent.adapters.sqlite.repositories import (
    BudgetRepository,
    TaskRepository,
    TraceRepository,
)


class SQLiteUnitOfWork:
    """A UoW that exposes fixed repository operations and atomic commit/rollback."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.tasks = TaskRepository(connection)
        self.trace = TraceRepository(connection)
        self.budget = BudgetRepository(connection)
        self._active = False

    def __enter__(self) -> SQLiteUnitOfWork:
        if self._active:
            raise RuntimeError("unit of work already active")
        self._connection.execute("BEGIN IMMEDIATE")
        self._active = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._active = False

    def execute_typed(self, operation: str, values: Mapping[str, object]) -> None:
        if operation != "insert_task":
            raise ValueError("unsupported persistence operation")
        if not self._active:
            raise RuntimeError("unit of work is not active")
        if operation == "insert_task":
            self.tasks.insert(values)
            return
        if operation == "insert_trace_event":
            self.trace.insert(values)
            return
        if operation == "insert_budget_entry":
            self.budget.insert_ledger_entry(values)
            return
