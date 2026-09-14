"""Recoverable orchestration for remote review side effects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from uuid import uuid4

from code_review_agent.application.persistence import ReviewStateStore
from code_review_agent.domain.common.digests import sha256_digest
from code_review_agent.domain.publication.models import (
    PublicationError,
    PublicationItemKind,
    PublicationItemResult,
    PublicationPlan,
    PublicationResult,
)
from code_review_agent.ports.publication import RemotePublisherPort


class PublicationService:
    def __init__(
        self,
        store: ReviewStateStore,
        *,
        scan: Callable[[str, str], str | None],
    ) -> None:
        self.store = store
        self._scan = scan

    def prepare(self, plan: PublicationPlan) -> PublicationResult:
        sanitized_items = []
        for item in plan.items:
            marker_suffix = f"\n\n{item.marker}"
            if not item.body.endswith(marker_suffix):
                raise ValueError("publication_content_invalid")
            visible = item.body[: -len(marker_suffix)]
            scanned = self._scan(visible, plan.task_id)
            if scanned is None:
                scanned = visible
            if not isinstance(scanned, str) or not scanned:
                raise ValueError("publication_content_rejected")
            if scanned == visible:
                sanitized_items.append(item)
                continue
            key = sha256_digest(
                {
                    "source_publication_key": item.publication_key,
                    "sanitized_body": sha256_digest(scanned),
                }
            )
            marker = f"<!-- cr-agent:publication:{key} -->"
            body = scanned.rstrip() + f"\n\n{marker}"
            sanitized_items.append(
                replace(
                    item,
                    publication_key=key,
                    marker=marker,
                    body=body,
                    body_digest=sha256_digest(body),
                )
            )
        plan = replace(plan, items=tuple(sanitized_items))
        self.store.save_publication_plan(plan)
        event_id = f"publication-planned-{plan.publication_id}"
        self.store.append_trace_event(
            task_id=plan.task_id,
            event_id=event_id,
            event_type="publication.planned",
            category="publication",
            summary={
                "publication_id": plan.publication_id,
                "item_count": len(plan.items),
                "platform": plan.target.platform,
                "snapshot_id": plan.snapshot_id,
            },
            idempotency_key=event_id,
        )
        return self.store.publication_result(plan.task_id)

    def get(self, task_id: str) -> PublicationResult:
        return self.store.publication_result(task_id)

    def publish(
        self, task_id: str, publisher: RemotePublisherPort
    ) -> PublicationResult:
        owner_id = str(uuid4())
        fencing_token = self.store.acquire_publication_lease(
            task_id, owner_id, seconds=600
        )
        if fencing_token is None:
            raise PublicationError("publication_lease_held")
        try:
            return self._publish_locked(
                task_id, publisher, owner_id, fencing_token
            )
        finally:
            self.store.release_publication_lease(
                task_id, owner_id, fencing_token
            )

    def _publish_locked(
        self,
        task_id: str,
        publisher: RemotePublisherPort,
        operation_id: str,
        fencing_token: int,
    ) -> PublicationResult:
        plan = self.store.publication_plan(task_id)
        current = self.store.publication_result(task_id)
        remaining = {
            item.publication_key: item
            for item in current.items
            if item.state != "succeeded"
        }
        skipped = len(current.items) - len(remaining)
        created = 0
        self._trace(
            task_id,
            operation_id,
            "publication.started",
            {"publication_id": plan.publication_id, "remaining": len(remaining)},
        )
        if not remaining:
            return self._finish(current, operation_id, created, skipped)
        self._renew(task_id, operation_id, fencing_token)
        try:
            target_state = publisher.verify_target(plan.target)
        except PublicationError as exc:
            return self._finish(
                self._fail_remaining(
                    plan,
                    remaining,
                    exc.code,
                    exc.outcome_unknown,
                    operation_id,
                    fencing_token,
                ),
                operation_id,
                created,
                skipped,
            )
        if not target_state.open:
            return self._finish(
                self._fail_remaining(
                    plan,
                    remaining,
                    "publication_target_closed",
                    False,
                    operation_id,
                    fencing_token,
                ),
                operation_id,
                created,
                skipped,
            )
        if target_state.head_sha != plan.target.head_sha:
            return self._finish(
                self._fail_remaining(
                    plan,
                    remaining,
                    "publication_target_changed",
                    False,
                    operation_id,
                    fencing_token,
                ),
                operation_id,
                created,
                skipped,
            )
        self._trace(
            task_id,
            operation_id,
            "publication.target_verified",
            {"head_sha_matches": True, "target_open": True},
        )
        markers = tuple(
            item.marker for item in plan.items if item.publication_key in remaining
        )
        self._renew(task_id, operation_id, fencing_token)
        try:
            found = publisher.find_markers(plan.target, markers)
        except PublicationError as exc:
            return self._finish(
                self._fail_remaining(
                    plan,
                    remaining,
                    exc.code,
                    exc.outcome_unknown,
                    operation_id,
                    fencing_token,
                    preserve_unknowns=True,
                ),
                operation_id,
                created,
                skipped,
            )
        self._trace(
            task_id,
            operation_id,
            "publication.reconciled",
            {"found": len(found), "searched": len(markers)},
        )
        for item in plan.items:
            if item.publication_key not in remaining:
                continue
            remote = found.get(item.marker)
            if remote is not None:
                self.store.update_publication_item(
                    task_id,
                    item.publication_key,
                    state="succeeded",
                    remote_id=remote.remote_id,
                    remote_url=remote.url,
                    owner_id=operation_id,
                    fencing_token=fencing_token,
                    expected_version=remaining[item.publication_key].version,
                )
                skipped += 1
                self._trace_item(
                    task_id, operation_id, item.publication_key, "succeeded"
                )
                continue
            self._renew(task_id, operation_id, fencing_token)
            visible = self._visible_body(item.body, item.marker)
            try:
                scanned = self._scan(visible, task_id)
            except Exception:
                scanned = ""
            if scanned is not None and scanned != visible:
                self.store.update_publication_item(
                    task_id,
                    item.publication_key,
                    state="failed",
                    error_code="publication_content_rejected",
                    owner_id=operation_id,
                    fencing_token=fencing_token,
                    expected_version=remaining[item.publication_key].version,
                )
                self._trace_item(
                    task_id,
                    operation_id,
                    item.publication_key,
                    "failed",
                    "publication_content_rejected",
                )
                continue
            try:
                remote = (
                    publisher.create_line_comment(plan.target, item)
                    if item.kind is PublicationItemKind.LINE
                    else publisher.create_summary_comment(plan.target, item)
                )
            except PublicationError as exc:
                self.store.update_publication_item(
                    task_id,
                    item.publication_key,
                    state="unknown" if exc.outcome_unknown else "failed",
                    error_code=exc.code,
                    owner_id=operation_id,
                    fencing_token=fencing_token,
                    expected_version=remaining[item.publication_key].version,
                )
                self._trace_item(
                    task_id,
                    operation_id,
                    item.publication_key,
                    "unknown" if exc.outcome_unknown else "failed",
                    exc.code,
                )
                continue
            self.store.update_publication_item(
                task_id,
                item.publication_key,
                state="succeeded",
                remote_id=remote.remote_id,
                remote_url=remote.url,
                owner_id=operation_id,
                fencing_token=fencing_token,
                expected_version=remaining[item.publication_key].version,
            )
            created += 1
            self._trace_item(task_id, operation_id, item.publication_key, "succeeded")
        return self._finish(
            self.store.publication_result(task_id), operation_id, created, skipped
        )

    def _renew(self, task_id: str, owner_id: str, fencing_token: int) -> None:
        if not self.store.renew_publication_lease(
            task_id, owner_id, fencing_token
        ):
            raise PublicationError("publication_lease_held")

    @staticmethod
    def _visible_body(body: str, marker: str) -> str:
        suffix = f"\n\n{marker}"
        if not body.endswith(suffix):
            raise ValueError("persistence_integrity_failed")
        return body[: -len(suffix)]

    def _fail_remaining(
        self,
        plan: PublicationPlan,
        remaining: Mapping[str, PublicationItemResult],
        code: str,
        unknown: bool,
        owner_id: str,
        fencing_token: int,
        *,
        preserve_unknowns: bool = False,
    ) -> PublicationResult:
        for item in plan.items:
            prior = remaining.get(item.publication_key)
            if prior is not None:
                if preserve_unknowns and getattr(prior, "state", None) == "unknown":
                    continue
                self.store.update_publication_item(
                    plan.task_id,
                    item.publication_key,
                    state="unknown" if unknown else "failed",
                    error_code=code,
                    owner_id=owner_id,
                    fencing_token=fencing_token,
                    expected_version=prior.version,
                )
        return self.store.publication_result(plan.task_id)

    def _finish(
        self,
        result: PublicationResult,
        operation_id: str,
        created: int,
        skipped: int,
    ) -> PublicationResult:
        self._trace(
            result.task_id,
            operation_id,
            "publication.completed",
            {
                "state": result.state,
                "published": created,
                "skipped": skipped,
                "failed": result.failed,
                "unknown": result.unknown,
            },
        )
        return PublicationResult(
            task_id=result.task_id,
            publication_id=result.publication_id,
            state=result.state,
            published=created,
            skipped=skipped,
            failed=result.failed,
            unknown=result.unknown,
            items=result.items,
        )

    def _trace_item(
        self,
        task_id: str,
        operation_id: str,
        publication_key: str,
        state: str,
        error_code: str | None = None,
    ) -> None:
        summary: dict[str, object] = {
            "publication_key": publication_key,
            "state": state,
        }
        if error_code is not None:
            summary["error_code"] = error_code
        self._trace(
            task_id,
            operation_id,
            f"publication.item_{state}",
            summary,
            suffix=publication_key,
        )

    def _trace(
        self,
        task_id: str,
        operation_id: str,
        event_type: str,
        summary: dict[str, object],
        *,
        suffix: str | None = None,
    ) -> None:
        key = f"publication-{operation_id}-{suffix or event_type}"
        self.store.append_trace_event(
            task_id=task_id,
            event_id=key,
            event_type=event_type,
            category="publication",
            summary=summary,
            idempotency_key=key,
        )
