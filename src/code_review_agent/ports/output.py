"""Output adapter contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from code_review_agent.domain.report.models import ReportModel


class OutputAdapterPort(Protocol):
    def render(self, model: ReportModel) -> str: ...

    def deliver(self, model: ReportModel, target: Path) -> DeliveryResult: ...


class SecurityBoundaryPort(Protocol):
    def scan_report(self, content: str, task_id: str) -> None: ...


class DeliveryResult:
    def __init__(self, path: Path, content_digest: str, delivery_key: str) -> None:
        self.path = path
        self.content_digest = content_digest
        self.delivery_key = delivery_key
