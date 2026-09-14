"""Small SQLite store for CLI-visible review state and recovery controls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from code_review_agent.adapters.sqlite.connection import connect_database
from code_review_agent.application.checkpoint_types import (
    require_trusted_checkpoint_type,
    resolve_checkpoint_type,
)
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
from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.publication.models import (
    PublicationItem,
    PublicationItemKind,
    PublicationItemResult,
    PublicationPlan,
    PublicationPosition,
    PublicationResult,
    PublicationTarget,
    aggregate_state,
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
                CREATE TABLE IF NOT EXISTS review_finding_traces (
                    trace_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    work_unit_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE(task_id, finding_id)
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
                CREATE TABLE IF NOT EXISTS review_publications (
                    task_id TEXT PRIMARY KEY,
                    publication_id TEXT NOT NULL UNIQUE,
                    snapshot_id TEXT NOT NULL,
                    snapshot_version INTEGER NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_publication_items (
                    publication_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    remote_id TEXT,
                    remote_url TEXT,
                    error_code TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(task_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS review_publication_leases (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_publication_fences (
                    task_id TEXT PRIMARY KEY,
                    last_token INTEGER NOT NULL
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
            for table, column, definition in (
                ("review_publication_items", "version", "INTEGER NOT NULL DEFAULT 1"),
                (
                    "review_publication_items",
                    "fencing_token",
                    "INTEGER NOT NULL DEFAULT 0",
                ),
                (
                    "review_publication_leases",
                    "fencing_token",
                    "INTEGER NOT NULL DEFAULT 0",
                ),
            ):
                try:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
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
            "publication": (
                None
                if result.publication is None
                else {
                    "task_id": result.publication.task_id,
                    "publication_id": result.publication.publication_id,
                    "state": result.publication.state,
                    "published": result.publication.published,
                    "skipped": result.publication.skipped,
                    "failed": result.publication.failed,
                    "unknown": result.publication.unknown,
                    "items": [
                        {
                            "publication_key": item.publication_key,
                            "kind": item.kind,
                            "state": item.state,
                            "remote_id": item.remote_id,
                            "remote_url": item.remote_url,
                            "error_code": item.error_code,
                            "version": item.version,
                        }
                        for item in result.publication.items
                    ],
                }
            ),
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
        publication = payload.get("publication")
        publication_result = None
        if publication is not None:
            publication_result = PublicationResult(
                task_id=publication["task_id"],
                publication_id=publication["publication_id"],
                state=publication["state"],
                published=int(publication["published"]),
                skipped=int(publication["skipped"]),
                failed=int(publication["failed"]),
                unknown=int(publication["unknown"]),
                items=tuple(
                    PublicationItemResult(**item) for item in publication["items"]
                ),
            )
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
            publication=publication_result,
        )

    def progress(self, task_id: str) -> ReviewProgressView:
        result = self.get(task_id)
        return ReviewProgressView(
            task_id=result.task_id,
            phase=result.phase,
            result_state=result.result_state,
            delivery_state=result.delivery_state,
            trace=result.trace,
            publication_state=(
                result.publication.state if result.publication is not None else None
            ),
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

    def save_publication_plan(self, plan: PublicationPlan) -> None:
        payload = self._publication_plan_json(plan)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT plan_json FROM review_publications WHERE task_id = ?",
                (plan.task_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != payload:
                    raise ValueError("publication_plan_conflict")
                return
            connection.execute(
                "INSERT INTO review_publications(task_id, publication_id, snapshot_id, "
                "snapshot_version, plan_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan.task_id,
                    plan.publication_id,
                    plan.snapshot_id,
                    plan.snapshot_version,
                    payload,
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO review_publication_items(publication_key, task_id, "
                "ordinal, kind, state) VALUES (?, ?, ?, ?, 'pending')",
                [
                    (item.publication_key, plan.task_id, index, item.kind.value)
                    for index, item in enumerate(plan.items)
                ],
            )

    def publication_plan(self, task_id: str) -> PublicationPlan:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT publication_id, snapshot_id, snapshot_version, plan_json "
                "FROM review_publications WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise ValueError("publication_not_found")
        try:
            plan = self._decode_publication_plan(str(row[3]))
            self._validate_publication_plan(plan)
            if (
                plan.task_id != task_id
                or plan.publication_id != str(row[0])
                or plan.snapshot_id != str(row[1])
                or plan.snapshot_version != int(row[2])
            ):
                raise ValueError("publication_binding_invalid")
            return plan
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("persistence_integrity_failed") from exc

    def publication_result(self, task_id: str) -> PublicationResult:
        plan = self.publication_plan(task_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT publication_key, kind, state, remote_id, remote_url, "
                "error_code, version FROM review_publication_items WHERE task_id = ? "
                "ORDER BY ordinal",
                (task_id,),
            ).fetchall()
        if len(rows) != len(plan.items):
            raise ValueError("persistence_integrity_failed")
        if any(
            str(row[0]) != planned.publication_key
            or str(row[1]) != planned.kind.value
            for row, planned in zip(rows, plan.items, strict=True)
        ):
            raise ValueError("persistence_integrity_failed")
        items = tuple(
            PublicationItemResult(
                publication_key=str(row[0]),
                kind=str(row[1]),
                state=str(row[2]),
                remote_id=str(row[3]) if row[3] is not None else None,
                remote_url=str(row[4]) if row[4] is not None else None,
                error_code=str(row[5]) if row[5] is not None else None,
                version=int(row[6]),
            )
            for row in rows
        )
        state = aggregate_state(items).value
        return PublicationResult(
            task_id=task_id,
            publication_id=plan.publication_id,
            state=state,
            published=sum(item.state == "succeeded" for item in items),
            skipped=0,
            failed=sum(item.state == "failed" for item in items),
            unknown=sum(item.state == "unknown" for item in items),
            items=items,
        )

    def update_publication_item(
        self,
        task_id: str,
        publication_key: str,
        *,
        state: str,
        remote_id: str | None = None,
        remote_url: str | None = None,
        error_code: str | None = None,
        owner_id: str,
        fencing_token: int,
        expected_version: int,
    ) -> None:
        if state not in {"pending", "succeeded", "failed", "unknown"}:
            raise ValueError("publication_state_invalid")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE review_publication_items SET state = ?, remote_id = ?, "
                "remote_url = ?, error_code = ?, version = version + 1, "
                "fencing_token = ? WHERE task_id = ? AND publication_key = ? "
                "AND version = ? AND EXISTS (SELECT 1 FROM "
                "review_publication_leases lease WHERE lease.task_id = ? "
                "AND lease.owner_id = ? AND lease.fencing_token = ? "
                "AND lease.expires_at > ?)",
                (
                    state,
                    remote_id,
                    remote_url,
                    error_code,
                    fencing_token,
                    task_id,
                    publication_key,
                    expected_version,
                    task_id,
                    owner_id,
                    fencing_token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("publication_fence_lost")

    def acquire_publication_lease(
        self, task_id: str, owner_id: str, *, seconds: int = 120
    ) -> int | None:
        now = datetime.now(UTC)
        expiry = now + timedelta(seconds=seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM review_publication_leases WHERE task_id = ? "
                "AND expires_at <= ?",
                (task_id, now.isoformat()),
            )
            cursor = connection.execute(
                "SELECT 1 FROM review_publication_leases WHERE task_id = ?",
                (task_id,),
            )
            if cursor.fetchone() is not None:
                return None
            row = connection.execute(
                "SELECT last_token FROM review_publication_fences WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            token = (int(row[0]) if row is not None else 0) + 1
            connection.execute(
                "INSERT INTO review_publication_fences(task_id, last_token) "
                "VALUES (?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                "last_token = excluded.last_token",
                (task_id, token),
            )
            connection.execute(
                "INSERT INTO review_publication_leases"
                "(task_id, owner_id, fencing_token, expires_at) VALUES (?, ?, ?, ?)",
                (task_id, owner_id, token, expiry.isoformat()),
            )
            return token

    def release_publication_lease(
        self, task_id: str, owner_id: str, fencing_token: int | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM review_publication_leases "
                "WHERE task_id = ? AND owner_id = ? "
                "AND (? IS NULL OR fencing_token = ?)",
                (task_id, owner_id, fencing_token, fencing_token),
            )

    def renew_publication_lease(
        self,
        task_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        seconds: int = 600,
    ) -> bool:
        now = datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE review_publication_leases SET expires_at = ? "
                "WHERE task_id = ? AND owner_id = ? AND fencing_token = ? "
                "AND expires_at > ?",
                (
                    (now + timedelta(seconds=seconds)).isoformat(),
                    task_id,
                    owner_id,
                    fencing_token,
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _publication_plan_json(plan: PublicationPlan) -> str:
        target = plan.target
        return json.dumps(
            {
                "publication_id": plan.publication_id,
                "task_id": plan.task_id,
                "snapshot_id": plan.snapshot_id,
                "snapshot_version": plan.snapshot_version,
                "target": {
                    "platform": target.platform,
                    "source_url": target.source_url,
                    "repository": target.repository,
                    "number": target.number,
                    "base_sha": target.base_sha,
                    "start_sha": target.start_sha,
                    "head_sha": target.head_sha,
                },
                "items": [
                    {
                        "publication_key": item.publication_key,
                        "kind": item.kind.value,
                        "body": item.body,
                        "body_digest": item.body_digest,
                        "marker": item.marker,
                        "finding_id": item.finding_id,
                        "position": None
                        if item.position is None
                        else {
                            "path": item.position.path,
                            "old_path": item.position.old_path,
                            "new_path": item.position.new_path,
                            "line": item.position.line,
                            "side": item.position.side,
                        },
                    }
                    for item in plan.items
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode_publication_plan(payload: str) -> PublicationPlan:
        raw = json.loads(payload)
        target = PublicationTarget(**raw["target"])
        items = []
        for value in raw["items"]:
            position = (
                PublicationPosition(**value["position"])
                if value["position"] is not None
                else None
            )
            items.append(
                PublicationItem(
                    publication_key=value["publication_key"],
                    kind=PublicationItemKind(value["kind"]),
                    body=value["body"],
                    body_digest=value["body_digest"],
                    marker=value["marker"],
                    finding_id=value["finding_id"],
                    position=position,
                )
            )
        return PublicationPlan(
            publication_id=raw["publication_id"],
            task_id=raw["task_id"],
            snapshot_id=raw["snapshot_id"],
            snapshot_version=int(raw["snapshot_version"]),
            target=target,
            items=tuple(items),
        )

    @staticmethod
    def _validate_publication_plan(plan: PublicationPlan) -> None:
        if not plan.task_id or not plan.snapshot_id or plan.snapshot_version <= 0:
            raise ValueError("publication_identity_invalid")
        for item in plan.items:
            expected_marker = (
                f"<!-- cr-agent:publication:{item.publication_key} -->"
            )
            if (
                item.marker != expected_marker
                or not item.body.endswith(f"\n\n{item.marker}")
                or sha256_digest(item.body) != item.body_digest
            ):
                raise ValueError("publication_item_integrity_failed")

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
            if rows:
                return tuple(
                    self._trace_event(connection, str(row[0])) for row in rows
                )
            link = connection.execute(
                "SELECT task_id, finding_id, work_unit_ids_json "
                "FROM review_finding_traces WHERE trace_id = ?",
                (task_id,),
            ).fetchone()
            if link is None:
                raise ValueError("trace_not_found")
            linked_task_id = str(link[0])
            finding_id = str(link[1])
            work_unit_ids = frozenset(json.loads(str(link[2])))
            linked_rows = connection.execute(
                "SELECT event_id FROM review_trace_events WHERE task_id = ? "
                "ORDER BY sequence",
                (linked_task_id,),
            ).fetchall()
            linked_events = tuple(
                self._trace_event(connection, str(row[0])) for row in linked_rows
            )
        relevant_categories = {"input", "planning", "report"}
        return tuple(
            event
            for event in linked_events
            if event.category in relevant_categories
            or event.summary.get("work_unit_id") in work_unit_ids
            or (
                event.event_type == "finding.validated"
                and event.summary.get("finding_id") == finding_id
            )
        )

    def link_finding_trace(
        self,
        *,
        trace_id: str,
        task_id: str,
        finding_id: str,
        work_unit_ids: tuple[str, ...],
        expires_at: datetime | None = None,
    ) -> None:
        if not trace_id or not task_id or not finding_id or not work_unit_ids:
            raise ValueError("finding_trace_fields_required")
        normalized_units = tuple(sorted(set(work_unit_ids)))
        created = datetime.now(UTC)
        requested_expiry = expires_at or created + timedelta(days=7)
        payload = json.dumps(normalized_units, separators=(",", ":"))
        with self._connect() as connection:
            event_expiry_row = connection.execute(
                "SELECT MIN(expires_at) FROM review_trace_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if event_expiry_row is None or event_expiry_row[0] is None:
                raise ValueError("finding_trace_events_required")
            event_expiry = datetime.fromisoformat(str(event_expiry_row[0]))
            expiry = min(requested_expiry.astimezone(UTC), event_expiry)
            existing = connection.execute(
                "SELECT task_id, finding_id, work_unit_ids_json "
                "FROM review_finding_traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            if existing is not None:
                if tuple(map(str, existing)) != (task_id, finding_id, payload):
                    raise ValueError("finding_trace_idempotency_conflict")
                return
            connection.execute(
                "INSERT INTO review_finding_traces "
                "(trace_id, task_id, finding_id, work_unit_ids_json, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    trace_id,
                    task_id,
                    finding_id,
                    payload,
                    created.isoformat(),
                    expiry.astimezone(UTC).isoformat(),
                ),
            )

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
            publication=result.publication,
        )
        self.save(updated)
        return updated

    def cleanup(self, task_id: str) -> None:
        self.get(task_id)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM review_publication_leases WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "DELETE FROM review_publication_fences WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "DELETE FROM review_publication_items WHERE task_id = ?", (task_id,)
            )
            connection.execute(
                "DELETE FROM review_publications WHERE task_id = ?", (task_id,)
            )
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
            "DELETE FROM review_finding_traces WHERE task_id = ?", (task_id,)
        )
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
            "DELETE FROM review_finding_traces WHERE expires_at <= ?", (timestamp,)
        )
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
                "__enum__": require_trusted_checkpoint_type(type(value)),
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
                "__dataclass__": require_trusted_checkpoint_type(type(value)),
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
            target = resolve_checkpoint_type(str(type_ref))
            if "__enum__" in value:
                return target(value["value"])
            raw_fields = value.get("fields")
            if not isinstance(raw_fields, dict):
                raise ValueError("checkpoint_corrupt")
            decoded = {
                str(key): cls._decode_checkpoint_value(item)
                for key, item in raw_fields.items()
            }
            return target(**decoded)
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
