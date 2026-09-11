import sqlite3
from pathlib import Path

import pytest

from code_review_agent.adapters.sqlite.connection import connect_database
from code_review_agent.adapters.sqlite.migrations import apply_migrations
from code_review_agent.adapters.sqlite.unit_of_work import SQLiteUnitOfWork


def test_database_connection_uses_required_durability_pragmas(tmp_path: Path) -> None:
    connection = connect_database(tmp_path / "state.sqlite3")

    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    connection.close()


def test_migrations_are_idempotent_and_record_schema_version(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    first = connect_database(database)
    apply_migrations(first)
    first.close()

    second = connect_database(database)
    apply_migrations(second)

    migration = second.execute(
        "SELECT version, name, checksum FROM schema_migrations"
    ).fetchone()
    assert migration[0] == 1
    assert migration[1] == "0001_initial"
    assert len(migration[2]) == 64
    assert second.execute("PRAGMA user_version").fetchone()[0] == 1
    assert second.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    second.close()


def test_foreign_keys_and_constraints_are_enforced(tmp_path: Path) -> None:
    connection = connect_database(tmp_path / "state.sqlite3")
    apply_migrations(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO input_bindings(binding_id, task_id, input_type, "
            "content_digest) "
            "VALUES (?, ?, ?, ?)",
            ("binding", "missing-task", "plain_diff", "digest"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO tasks(task_id, spec_id, control_state, phase, result_state, "
            "delivery_state, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("task", "spec", "invalid", "created", "pending", "not_ready", 1),
        )
    connection.close()


def test_uow_commit_and_rollback_are_atomic(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    connection = connect_database(database)
    apply_migrations(connection)
    uow = SQLiteUnitOfWork(connection)

    with uow:
        uow.execute_typed(
            "insert_task",
            {
                "task_id": "task-1",
                "spec_id": "spec-1",
                "control_state": "ready",
                "phase": "created",
                "result_state": "pending",
                "delivery_state": "not_ready",
                "version": 1,
            },
        )
    assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1

    with pytest.raises(RuntimeError), uow:
        uow.execute_typed(
            "insert_task",
            {
                "task_id": "task-2",
                "spec_id": "spec-2",
                "control_state": "ready",
                "phase": "created",
                "result_state": "pending",
                "delivery_state": "not_ready",
                "version": 1,
            },
        )
        raise RuntimeError("rollback")
    assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    connection.close()


def test_uow_rejects_arbitrary_sql_and_repositories_do_not_expose_execute(
    tmp_path: Path,
) -> None:
    connection = connect_database(tmp_path / "state.sqlite3")
    apply_migrations(connection)
    uow = SQLiteUnitOfWork(connection)

    with pytest.raises(ValueError):
        uow.execute_typed("DROP TABLE tasks", {})
    assert not hasattr(uow.tasks, "execute")
    assert not hasattr(uow.trace, "execute")
    assert not hasattr(uow.budget, "execute")
    connection.close()
