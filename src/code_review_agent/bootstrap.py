"""Composition root for the thin CLI adapter."""

from __future__ import annotations

from typing import Protocol

from code_review_agent.application.dto import (
    ReviewProgressView,
    ReviewRunResult,
    StartReviewCommand,
)
from code_review_agent.config import CliConfig


class CliRuntime(Protocol):
    def review(
        self,
        command: StartReviewCommand,
        provider: str,
        model: str,
        budget_tokens: int,
        request_id: str,
    ) -> ReviewRunResult: ...

    def status(self, task_id: str) -> ReviewProgressView: ...

    def trace(self, trace_id: str) -> tuple[object, ...]: ...


class UnavailableRuntime:
    def __init__(self, config: CliConfig) -> None:
        self.config = config

    def review(
        self,
        command: StartReviewCommand,
        provider: str,
        model: str,
        budget_tokens: int,
        request_id: str,
    ) -> ReviewRunResult:
        raise RuntimeError("application_runtime_not_configured")

    def status(self, task_id: str) -> ReviewProgressView:
        raise ValueError("task_not_found")

    def trace(self, trace_id: str) -> tuple[object, ...]:
        raise ValueError("trace_not_found")


def build_runtime() -> CliRuntime:
    return UnavailableRuntime(CliConfig.from_environment())
