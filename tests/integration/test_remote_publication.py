from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import respx
from pytest import MonkeyPatch

from code_review_agent.application.dto import ReviewRunResult, StartReviewCommand
from code_review_agent.application.persistence import ReviewStateStore
from code_review_agent.application.publication_service import PublicationService
from code_review_agent.bootstrap import ConfiguredRuntime
from code_review_agent.config import CliConfig
from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.publication.models import (
    PublicationError,
    PublicationItem,
    PublicationItemKind,
    PublicationPlan,
    PublicationPosition,
    PublicationTarget,
    RemoteComment,
    RemoteTargetState,
)


def _plan() -> PublicationPlan:
    target = PublicationTarget(
        "github",
        "https://github.com/owner/repo/pull/7",
        "owner/repo",
        7,
        "a" * 40,
        "a" * 40,
        "b" * 40,
    )
    line_body = "line body\n\n<!-- cr-agent:publication:line-key -->"
    line = PublicationItem(
        "line-key",
        PublicationItemKind.LINE,
        line_body,
        sha256_digest(line_body),
        "<!-- cr-agent:publication:line-key -->",
        "finding-1",
        PublicationPosition("a.py", "a.py", "a.py", 2, "RIGHT"),
    )
    summary_body = "summary\n\n<!-- cr-agent:publication:summary-key -->"
    summary = PublicationItem(
        "summary-key",
        PublicationItemKind.SUMMARY,
        summary_body,
        sha256_digest(summary_body),
        "<!-- cr-agent:publication:summary-key -->",
    )
    return PublicationPlan(
        "publication-1", "task-1", "snapshot-1", 1, target, (line, summary)
    )


class Publisher:
    def __init__(
        self,
        *,
        fail_line: bool = False,
        unknown_summary: bool = False,
        found: dict[str, RemoteComment] | None = None,
    ) -> None:
        self.fail_line = fail_line
        self.unknown_summary = unknown_summary
        self.found = found or {}
        self.created_keys: list[str] = []

    def verify_target(self, target: PublicationTarget) -> RemoteTargetState:
        return RemoteTargetState(True, target.head_sha)

    def find_markers(
        self, target: PublicationTarget, markers: tuple[str, ...]
    ) -> dict[str, RemoteComment]:
        return {
            marker: value for marker, value in self.found.items() if marker in markers
        }

    def create_line_comment(
        self, target: PublicationTarget, item: PublicationItem
    ) -> RemoteComment:
        self.created_keys.append(item.publication_key)
        if self.fail_line:
            raise PublicationError("publication_position_invalid")
        return RemoteComment("remote-line", "https://example/line")

    def create_summary_comment(
        self, target: PublicationTarget, item: PublicationItem
    ) -> RemoteComment:
        self.created_keys.append(item.publication_key)
        if self.unknown_summary:
            raise PublicationError("publication_result_unknown", outcome_unknown=True)
        return RemoteComment("remote-summary", "https://example/summary")


def _service(path: Path) -> PublicationService:
    return PublicationService(ReviewStateStore(path), scan=lambda body, task: None)


def test_partial_publication_resumes_without_duplicate_remote_writes(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "state.sqlite3")
    service.prepare(_plan())

    first_publisher = Publisher(fail_line=True)
    first = service.publish("task-1", first_publisher)
    second_publisher = Publisher()
    second = service.publish("task-1", second_publisher)

    assert first.state == "partial"
    assert first_publisher.created_keys == ["line-key", "summary-key"]
    assert second.state == "succeeded"
    assert second_publisher.created_keys == ["line-key"]


def test_unknown_write_is_reconciled_by_marker_before_retry(tmp_path: Path) -> None:
    service = _service(tmp_path / "state.sqlite3")
    service.prepare(_plan())
    first = service.publish("task-1", Publisher(unknown_summary=True))
    found = {
        "<!-- cr-agent:publication:summary-key -->": RemoteComment(
            "remote-summary", "https://example/summary"
        )
    }
    reconciler = Publisher(found=found)

    second = service.publish("task-1", reconciler)

    assert first.state == "unknown"
    assert second.state == "succeeded"
    assert reconciler.created_keys == []


def test_unknown_state_survives_failed_reconciliation(tmp_path: Path) -> None:
    service = _service(tmp_path / "state.sqlite3")
    service.prepare(_plan())
    service.publish("task-1", Publisher(unknown_summary=True))

    class ListingFailure(Publisher):
        def find_markers(
            self, target: PublicationTarget, markers: tuple[str, ...]
        ) -> dict[str, RemoteComment]:
            raise PublicationError("publication_provider_unavailable")

    result = service.publish("task-1", ListingFailure())

    assert result.state == "unknown"
    assert result.unknown == 1


def test_completed_publication_is_a_no_network_noop(tmp_path: Path) -> None:
    service = _service(tmp_path / "state.sqlite3")
    service.prepare(_plan())
    service.publish("task-1", Publisher())

    class NoNetwork(Publisher):
        def verify_target(self, target: PublicationTarget) -> RemoteTargetState:
            raise AssertionError("completed publication performed network I/O")

    result = service.publish("task-1", NoNetwork())

    assert result.state == "succeeded"
    assert result.published == 0
    assert result.skipped == 2


def test_publication_lease_excludes_concurrent_writer(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")
    service = PublicationService(store, scan=lambda body, task: None)
    service.prepare(_plan())
    assert store.acquire_publication_lease("task-1", "other-owner")

    try:
        service.publish("task-1", Publisher())
    except PublicationError as exc:
        assert exc.code == "publication_lease_held"
    else:
        raise AssertionError("concurrent publication acquired the same lease")


def test_stale_fencing_token_cannot_update_after_new_owner_acquires(
    tmp_path: Path,
) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")
    PublicationService(store, scan=lambda body, task: None).prepare(_plan())
    first = store.acquire_publication_lease("task-1", "owner-1")
    assert first
    store.release_publication_lease("task-1", "owner-1")
    second = store.acquire_publication_lease("task-1", "owner-2")
    assert second and second > first

    try:
        store.update_publication_item(
            "task-1",
            "line-key",
            state="succeeded",
            owner_id="owner-1",
            fencing_token=first,
            expected_version=1,
        )
    except ValueError as exc:
        assert str(exc) == "publication_fence_lost"
    else:
        raise AssertionError("stale owner updated publication state")


def test_cleanup_removes_local_publication_state(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.sqlite3")
    service = PublicationService(store, scan=lambda body, task: None)
    service.prepare(_plan())
    store.save(
        ReviewRunResult(
            task_id="task-1",
            session_id="session-1",
            phase="completed",
            result_state="complete_no_findings",
            delivery_state="succeeded",
            report_path=tmp_path / "report.md",
            report_digest="digest",
        )
    )

    store.cleanup("task-1")

    try:
        service.get("task-1")
    except ValueError as exc:
        assert str(exc) == "publication_not_found"
    else:
        raise AssertionError("cleanup retained publication state")


def test_changed_remote_head_fails_before_any_write(tmp_path: Path) -> None:
    service = _service(tmp_path / "state.sqlite3")
    service.prepare(_plan())
    publisher = Publisher()
    publisher.verify_target = lambda target: RemoteTargetState(True, "c" * 40)  # type: ignore[method-assign]

    result = service.publish("task-1", publisher)

    assert result.state == "failed"
    assert {item.error_code for item in result.items} == {"publication_target_changed"}
    assert publisher.created_keys == []


def test_security_rejection_prevents_plan_persistence(tmp_path: Path) -> None:
    def reject(body: str, task_id: str) -> None:
        raise ValueError("publication_content_rejected")

    service = PublicationService(
        ReviewStateStore(tmp_path / "state.sqlite3"), scan=reject
    )

    try:
        service.prepare(_plan())
    except ValueError as exc:
        assert str(exc) == "publication_content_rejected"
    else:
        raise AssertionError("unsafe plan was persisted")

    try:
        service.get("task-1")
    except ValueError as exc:
        assert str(exc) == "publication_not_found"
    else:
        raise AssertionError("rejected publication was found")


def test_security_redaction_rekeys_and_persists_only_sanitized_body(
    tmp_path: Path,
) -> None:
    def redact(body: str, task_id: str) -> str:
        return body.replace("line body", "safe body")

    service = PublicationService(
        ReviewStateStore(tmp_path / "state.sqlite3"), scan=redact
    )

    service.prepare(_plan())
    stored = service.store.publication_plan("task-1")

    assert stored.items[0].publication_key != "line-key"
    assert "line body" not in stored.items[0].body
    assert "safe body" in stored.items[0].body
    assert stored.items[0].marker in stored.items[0].body


def test_tampered_persisted_body_fails_integrity_before_network(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    service = _service(database)
    service.prepare(_plan())
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT plan_json FROM review_publications WHERE task_id = ?",
                ("task-1",),
            ).fetchone()[0]
        )
        payload["items"][0]["body"] = "tampered body"
        connection.execute(
            "UPDATE review_publications SET plan_json = ? WHERE task_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), "task-1"),
        )

    try:
        service.publish("task-1", Publisher())
    except ValueError as exc:
        assert str(exc) == "persistence_integrity_failed"
    else:
        raise AssertionError("tampered publish body reached publication")


def test_publish_rescans_persisted_body_before_each_remote_write(
    tmp_path: Path,
) -> None:
    preparing = True

    def changing_policy(body: str, task_id: str) -> str:
        return body if preparing else body.replace("line body", "redacted body")

    service = PublicationService(
        ReviewStateStore(tmp_path / "state.sqlite3"), scan=changing_policy
    )
    service.prepare(_plan())
    preparing = False
    publisher = Publisher()

    result = service.publish("task-1", publisher)

    assert result.state == "partial"
    assert result.items[0].error_code == "publication_content_rejected"
    assert publisher.created_keys == ["summary-key"]


@respx.mock
def test_configured_runtime_reviews_remote_input_and_publishes_summary(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    class InputCredentials:
        def get(self, *, provider_id: str, alias: str) -> str:
            return "token"

    metadata_url = "https://api.github.com/repos/owner/repo/pulls/7"
    respx.get(metadata_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "open",
                "merged_at": None,
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
                "url": metadata_url,
            },
        )
    )
    respx.get(metadata_url + "/files?per_page=100").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "filename": "a.py",
                    "status": "modified",
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ],
        )
    )
    monkeypatch.setattr(
        "code_review_agent.bootstrap.KeyringCredentialStore", InputCredentials
    )
    runtime = ConfiguredRuntime(CliConfig(state_database=tmp_path / "state.sqlite3"))
    publisher = Publisher()
    monkeypatch.setattr(runtime, "_publisher", lambda platform: publisher)

    result = runtime.review(
        StartReviewCommand(
            task_id="runtime-task",
            diff_text=None,
            diff_file=None,
            output_path=tmp_path / "report.md",
            source_url="https://github.com/owner/repo/pull/7",
            publish=True,
        ),
        "local",
        "deterministic",
        "request-1",
    )

    assert result.publication is not None
    assert result.publication.state == "succeeded"
    assert publisher.created_keys and len(publisher.created_keys) == 1
    assert runtime.publish("runtime-task").state == "succeeded"
    assert len(publisher.created_keys) == 1
