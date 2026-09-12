"""Configuration-backed composition root for one local review."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol
from uuid import uuid4

from code_review_agent.adapters.input.plain_diff import PlainDiffProvider
from code_review_agent.adapters.model.openai_compatible import (
    OpenAICompatibleProvider,
)
from code_review_agent.adapters.output.markdown import MarkdownOutputAdapter
from code_review_agent.adapters.security.scanner import (
    FixedSecurityScanner,
    load_packaged_security_policy,
)
from code_review_agent.adapters.tools.registry import ToolRegistry
from code_review_agent.application.dto import (
    ReviewProgressView,
    ReviewRunResult,
    StartReviewCommand,
)
from code_review_agent.application.orchestration import (
    ReviewDependencies,
    ReviewOrchestrator,
)
from code_review_agent.application.task_service import LocalDiffReviewService
from code_review_agent.config import CliConfig
from code_review_agent.domain.budget.service import BudgetService
from code_review_agent.domain.common.digests import canonical_json, sha256_bytes
from code_review_agent.domain.common.time import SystemClock
from code_review_agent.domain.execution.execution_models import (
    WorkUnitExecutionResult,
)
from code_review_agent.domain.execution.executor import (
    ExecutionRequest,
    WorkUnitExecutor,
)
from code_review_agent.domain.execution.models import (
    ModelCapabilities,
    ModelRequestOptions,
    ModelUsage,
    PreparedModelRequest,
    PromptEnvelope,
    ProviderSendResult,
    ProviderState,
    StructuredOutputStrategy,
    UsageState,
)
from code_review_agent.domain.findings.models import FindingProcessingRequest
from code_review_agent.domain.findings.processor import FindingProcessor
from code_review_agent.domain.input.service import InputService
from code_review_agent.domain.planning.models import (
    ModelCapacitySummary,
    PlanningStrategy,
)
from code_review_agent.domain.planning.planner import PlanningRequest, ReviewPlanner
from code_review_agent.domain.report.builder import ReportBuilder
from code_review_agent.domain.report.models import ResultSnapshot, ResultSnapshotKind
from code_review_agent.domain.security.models import ArtifactPurpose
from code_review_agent.domain.security.service import SecurityService
from code_review_agent.domain.task.models import TaskSpec


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


class LocalModelProvider:
    """Offline model provider used by the configured local MVP."""

    def __init__(self, config: CliConfig) -> None:
        self._capabilities = ModelCapabilities(
            provider_id=config.provider_id,
            provider_version=config.provider_version,
            model_id=config.model_id,
            origin=config.provider_origin,
            context_token_limit=10_000,
            max_output_tokens=config.max_output_tokens,
            structured_output_strategies=(StructuredOutputStrategy.JSON_SCHEMA,),
            preflight_token_counting=True,
            usage_mapping_trusted=True,
            streaming_disabled=True,
            retries_disabled=True,
            dynamic_tools_disabled=True,
            request_method="POST",
            request_path="/review",
            fixed_headers={
                "accept": "application/json",
                "content-type": "application/json",
            },
        )
        self._prepared: dict[str, PreparedModelRequest] = {}

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def prepare_request(
        self, envelope: PromptEnvelope, options: ModelRequestOptions
    ) -> PreparedModelRequest:
        body = canonical_json(
            {"diff": envelope.diff, "output_schema": dict(envelope.output_schema)}
        ).encode("utf-8")
        request = PreparedModelRequest(
            provider_id=self._capabilities.provider_id,
            provider_version=self._capabilities.provider_version,
            model_id=self._capabilities.model_id,
            origin=self._capabilities.origin,
            method="POST",
            path="/review",
            headers=self._capabilities.fixed_headers,
            body=body,
            body_digest=sha256_bytes(body),
            preparation_id=str(uuid4()),
            strategy=options.strategy,
            input_token_bound=300,
            output_token_max=options.max_output_tokens,
            streaming=False,
            automatic_retries=0,
            fallback_model_id=None,
            dynamic_tools=(),
        )
        self._prepared[request.preparation_id] = request
        return request

    def owns_prepared_request(self, request: PreparedModelRequest) -> bool:
        return self._prepared.get(request.preparation_id) is request

    def discard_prepared(self, request: PreparedModelRequest) -> None:
        self._prepared.pop(request.preparation_id, None)

    async def send_prepared(self, request: PreparedModelRequest) -> ProviderSendResult:
        if not self.owns_prepared_request(request):
            raise ValueError("request has no valid prepare permit")
        self.discard_prepared(request)
        return ProviderSendResult(
            provider_state=ProviderState.SUCCEEDED,
            request_sent=True,
            response_payload={"findings": []},
            usage=ModelUsage(UsageState.KNOWN, input_tokens=1, output_tokens=1),
        )


@dataclass(slots=True)
class _ConfiguredSteps:
    config: CliConfig
    task_id: str
    security: SecurityService
    input_service: InputService
    budget: BudgetService
    budget_account_id: str
    provider: Any
    tool_registry: ToolRegistry
    normalized: Any = None
    diff_text: str = ""

    def normalize(self, command: StartReviewCommand) -> Any:
        if command.source_url is not None:
            raise ValueError("url_provider_not_configured")
        self.normalized = self.input_service.normalize_plain_diff(
            task_id=self.task_id,
            text=command.diff_text,
            file_path=command.diff_file,
        )
        self.diff_text = self.security.resolve(
            self.normalized.change_set.sanitized_diff_ref,
            expected_purpose=ArtifactPurpose.DOMAIN_INGRESS,
        )
        return self.normalized

    def plan(self, normalized: Any) -> Any:
        task_spec = TaskSpec(
            spec_id="config-spec-1",
            input_intent="plain_diff",
            provider_id=self.config.provider_id,
            provider_version=self.config.provider_version,
            model_id=self.config.model_id,
            provider_origin=self.config.provider_origin,
            ruleset_id=self.config.ruleset_id,
            ruleset_version=self.config.ruleset_version,
            tools=(),
            security_policy_id=self.config.security_policy_id,
            security_policy_version=self.config.security_policy_version,
            config_digest=sha256_bytes(b"code-review-agent.toml"),
            credential_alias="none",
            budget_account_id=self.budget_account_id,
        )
        capabilities = self.provider.capabilities
        return ReviewPlanner().plan(
            PlanningRequest(
                task_id=self.task_id,
                task_spec=task_spec,
                binding=normalized.binding,
                change_set=normalized.change_set,
                strategy=PlanningStrategy("file_first_v1", "1"),
                model_capacity=ModelCapacitySummary(
                    provider_id=capabilities.provider_id,
                    provider_version=capabilities.provider_version,
                    model_id=capabilities.model_id,
                    context_token_limit=capabilities.context_token_limit,
                    max_output_tokens=capabilities.max_output_tokens,
                    token_counting_version="v1",
                ),
                tool_catalog=self.tool_registry.freeze(),
            )
        ).plan

    async def execute(self, unit: Any) -> WorkUnitExecutionResult:
        return await WorkUnitExecutor().execute(
            ExecutionRequest(
                task_id=self.task_id,
                work_unit=unit,
                attempt_number=1,
                model_provider=self.provider,
                model_options_strategy=StructuredOutputStrategy.JSON_SCHEMA,
                max_output_tokens=self.config.max_output_tokens,
                budget_service=self.budget,
                budget_account_id=self.budget_account_id,
                security_service=self.security,
                tool_catalog=self.tool_registry,
                system_rules="Review only the supplied change.",
                diff_text=self.diff_text,
            )
        )

    def consolidate(
        self, normalized: Any, plan: Any, executions: tuple[Any, ...]
    ) -> Any:
        return FindingProcessor().process(
            FindingProcessingRequest(
                request_id=f"consolidate-{self.task_id}",
                task_id=self.task_id,
                execution_fact_boundary_id=f"boundary-{self.task_id}",
                change_set=normalized.change_set,
                plan=plan,
                executions=executions,
            )
        ).finding_set

    def snapshot(
        self,
        command: StartReviewCommand,
        normalized: Any,
        plan: Any,
        executions: tuple[Any, ...],
        finding_set: Any,
    ) -> ResultSnapshot:
        result_state = (
            "no_changes"
            if not normalized.change_set.files
            else "complete_with_findings"
            if finding_set.findings
            else "complete_no_findings"
        )
        return ResultSnapshot(
            snapshot_id=f"snapshot-{self.task_id}",
            task_id=self.task_id,
            snapshot_version=1,
            kind=ResultSnapshotKind.REVIEW,
            result_state=result_state,
            input_binding=normalized.binding,
            review_plan=plan,
            finding_set=finding_set,
            budget_summary=self.budget.get_summary(self.budget_account_id),
            unknown_attempts=(),
            trace_summary=SimpleNamespace(task_id=self.task_id),
            checkpoint_id=f"boundary-{self.task_id}",
            subject=command.subject,
            security_summary="fixed local security policy",
        )

    def report(self, snapshot: ResultSnapshot) -> Any:
        return ReportBuilder().build(snapshot)

    def deliver(self, model: Any, target: Path) -> Any:
        return MarkdownOutputAdapter().deliver(model, target)


class ConfiguredRuntime:
    def __init__(self, config: CliConfig) -> None:
        self.config = config
        self.orchestrator = ReviewOrchestrator()
        self.tasks = LocalDiffReviewService(self.orchestrator)

    def review(
        self,
        command: StartReviewCommand,
        provider: str,
        model: str,
        budget_tokens: int,
        request_id: str,
    ) -> ReviewRunResult:
        del request_id
        if provider != self.config.provider_id or model != self.config.model_id:
            raise ValueError("provider_or_model_not_configured")
        policy = load_packaged_security_policy()
        security = SecurityService(FixedSecurityScanner(), policy=policy)
        budget = BudgetService()
        account = budget.create_account(
            command.task_id, budget_tokens, capability_ref="capability-1"
        )
        if self.config.provider_id == "openai-compatible":
            model_provider: Any = OpenAICompatibleProvider(self.config)
        else:
            model_provider = LocalModelProvider(self.config)
        steps = _ConfiguredSteps(
            config=self.config,
            task_id=command.task_id,
            security=security,
            input_service=InputService(
                PlainDiffProvider(security), security, clock=SystemClock()
            ),
            budget=budget,
            budget_account_id=account.account_id,
            provider=model_provider,
            tool_registry=ToolRegistry.from_builtin(),
        )
        return asyncio.run(self.tasks.start(command, ReviewDependencies(steps)))

    def status(self, task_id: str) -> ReviewProgressView:
        from code_review_agent.application.query_service import ReviewQueryService

        return ReviewQueryService(self.orchestrator, self.tasks).get_progress(task_id)

    def trace(self, trace_id: str) -> tuple[object, ...]:
        from code_review_agent.application.query_service import ReviewQueryService

        return ReviewQueryService(self.orchestrator, self.tasks).get_trace(trace_id)


def build_runtime(config: CliConfig | None = None) -> CliRuntime:
    selected = config or CliConfig.from_file(Path("code-review-agent.toml"))
    return ConfiguredRuntime(selected)
