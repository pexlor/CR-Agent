"""Deterministic Markdown rendering and atomic local delivery."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from code_review_agent.domain.common.digests import sha256_bytes, sha256_digest
from code_review_agent.domain.report.models import ReportModel
from code_review_agent.ports.output import DeliveryResult


class MarkdownOutputAdapter:
    adapter_id = "markdown"
    adapter_version = "1"

    def __init__(self, *, security_boundary: Any = None) -> None:
        self._security_boundary = security_boundary
        template_path = (
            Path(__file__).parents[2] / "resources" / "templates" / "review.md.j2"
        )
        self._template = Environment(
            undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
        ).from_string(template_path.read_text(encoding="utf-8"))

    def render(self, model: ReportModel) -> str:
        content = self._template.render(
            task_id=model.task_id,
            snapshot_id=model.snapshot_id,
            snapshot_version=model.snapshot_version,
            subject=model.subject,
            completion=model.completion,
            coverage=model.coverage,
            findings=model.findings,
            budget=model.budget,
            trace=model.trace,
        )
        return content.replace("\r\n", "\n").rstrip("\n") + "\n"

    def deliver(self, model: ReportModel, target: Path) -> DeliveryResult:
        content = self.render(model)
        if self._security_boundary is not None:
            self._security_boundary.scan_report(content, model.task_id)
        encoded = content.encode("utf-8")
        digest = sha256_bytes(encoded)
        target.parent.mkdir(parents=True, exist_ok=True)
        delivery_key = sha256_digest(
            {
                "task_id": model.task_id,
                "snapshot_id": model.snapshot_id,
                "adapter": [self.adapter_id, self.adapter_version],
                "target": str(target),
                "digest": digest,
            }
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            if sha256_bytes(target.read_bytes()) != digest:
                raise ValueError("delivery_artifact_mismatch")
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return DeliveryResult(target, digest, delivery_key)
