"""In-memory application service for starting local review runs."""

from __future__ import annotations

from dataclasses import dataclass, replace

from code_review_agent.application.dto import ReviewRunResult, StartReviewCommand
from code_review_agent.application.orchestration import (
    ReviewDependencies,
    ReviewOrchestrator,
)
from code_review_agent.application.persistence import ReviewStateStore


@dataclass(slots=True)
class LocalDiffReviewService:
    orchestrator: ReviewOrchestrator
    _runs: dict[str, ReviewRunResult] | None = None
    _pause_requests: set[str] | None = None
    _unknown_tasks: set[str] | None = None
    _commands: dict[str, StartReviewCommand] | None = None
    _dependencies: dict[str, ReviewDependencies] | None = None
    _terminated: set[str] | None = None
    store: ReviewStateStore | None = None

    def __post_init__(self) -> None:
        if self._runs is None:
            self._runs = {}
        if self._pause_requests is None:
            self._pause_requests = set()
        if self._unknown_tasks is None:
            self._unknown_tasks = set()
        if self._commands is None:
            self._commands = {}
        if self._dependencies is None:
            self._dependencies = {}
        if self._terminated is None:
            self._terminated = set()

    async def start(
        self,
        command: StartReviewCommand,
        dependencies: ReviewDependencies,
    ) -> ReviewRunResult:
        assert self._pause_requests is not None
        assert self._commands is not None
        assert self._dependencies is not None
        pause_requests = self._pause_requests
        self._commands[command.task_id] = command
        self._dependencies[command.task_id] = dependencies
        if self.store is not None:
            self.store.save_command(
                command,
                command.provider,
                command.model,
                command.budget_tokens,
            )
        result = await self.orchestrator.run(
            command,
            replace(
                dependencies,
                should_stop=lambda: (
                    dependencies.should_stop() or command.task_id in pause_requests
                ),
            ),
        )
        if command.task_id in self._pause_requests:
            result = replace(result, limitations=("pause_requested",))
        assert self._runs is not None
        self._runs[result.task_id] = result
        if self.store is not None:
            self.store.save(result)
        return result

    def request_pause(self, task_id: str, *, reason: str) -> None:
        if not reason:
            raise ValueError("pause_reason_required")
        assert self._pause_requests is not None
        self._pause_requests.add(task_id)

    async def resume(
        self,
        task_id: str,
        dependencies: ReviewDependencies | None = None,
        *,
        confirm_unknown_retry: bool = False,
    ) -> ReviewRunResult:
        self.can_resume(task_id, confirm_unknown_retry=confirm_unknown_retry)
        assert self._pause_requests is not None
        assert self._commands is not None
        assert self._dependencies is not None
        command = self._commands.get(task_id)
        if command is None and self.store is not None:
            command, _, _, _ = self.store.command(task_id)
        selected = dependencies or self._dependencies.get(task_id)
        if command is None or selected is None:
            raise ValueError("resume_context_not_found")
        self._pause_requests.discard(task_id)
        return await self.start(command, selected)

    def terminate(self, task_id: str, *, reason: str) -> ReviewRunResult:
        if not reason:
            raise ValueError("termination_reason_required")
        if self.store is not None:
            return self.store.terminate(task_id)
        assert self._terminated is not None
        result = self.get(task_id)
        updated = replace(
            result,
            phase="terminated",
            limitations=tuple((*result.limitations, "terminated")),
        )
        self._terminated.add(task_id)
        assert self._runs is not None
        self._runs[task_id] = updated
        return updated

    def cleanup(self, task_id: str) -> None:
        if self.store is not None:
            self.store.cleanup(task_id)
            return
        assert self._runs is not None
        result = self._runs.pop(task_id, None)
        if result is None:
            raise ValueError("task_not_found")
        if result.report_path is not None and not result.report_path.exists():
            raise ValueError("report_not_found")

    def mark_unknown(self, task_id: str) -> None:
        assert self._unknown_tasks is not None
        self._unknown_tasks.add(task_id)

    def can_resume(self, task_id: str, *, confirm_unknown_retry: bool = False) -> bool:
        assert self._unknown_tasks is not None
        if task_id in self._unknown_tasks and not confirm_unknown_retry:
            raise ValueError("unknown_retry_confirmation_required")
        return True

    def get(self, task_id: str) -> ReviewRunResult:
        if self.store is not None:
            return self.store.get(task_id)
        assert self._runs is not None
        try:
            return self._runs[task_id]
        except KeyError as exc:
            raise ValueError("task_not_found") from exc


ReviewTaskService = LocalDiffReviewService
