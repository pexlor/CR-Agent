"""Checksum-verified SQLite migrations."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class MigrationError(RuntimeError):
    """The migration history cannot be trusted or applied."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str


def discover_migrations(directory: str | Path | None = None) -> tuple[Migration, ...]:
    migration_dir = (
        Path(directory) if directory else Path(__file__).parents[2] / "migrations"
    )
    migrations: list[Migration] = []
    for path in sorted(migration_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        prefix, _, stem = path.stem.partition("_")
        version = int(prefix)
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(version, path.stem, hashlib.sha256(sql.encode()).hexdigest(), sql)
        )
    return tuple(migrations)


def apply_migrations(
    connection: sqlite3.Connection, directory: str | Path | None = None
) -> None:
    migrations = discover_migrations(directory)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )
    applied = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        )
    }
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if user_version != max(applied, default=0):
        raise MigrationError("schema version mismatch")
    for migration in migrations:
        if migration.version in applied:
            if applied[migration.version] != (migration.name, migration.checksum):
                raise MigrationError(
                    f"migration checksum mismatch: {migration.version}"
                )
            continue
        if migration.version != user_version + 1:
            raise MigrationError("migration version gap")
        try:
            escaped_name = migration.name.replace("'", "''")
            applied_at = datetime.now(UTC).isoformat().replace("'", "''")
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                f"{migration.sql}\n"
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                f"VALUES ({migration.version}, '{escaped_name}', "
                f"'{migration.checksum}', '{applied_at}');\n"
                f"PRAGMA user_version={migration.version};\n"
                "COMMIT;"
            )
            user_version = migration.version
        except sqlite3.Error as exc:
            connection.rollback()
            raise MigrationError("migration failed") from exc
