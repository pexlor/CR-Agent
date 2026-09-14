"""Configuration-backed composition root for one local review."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from code_review_agent.adapters.credentials.keyring_store import KeyringCredentialStore
from code_review_agent.adapters.input.github import GitHubInputProvider
from code_review_agent.adapters.input.gitlab import GitLabInputProvider
from code_review_agent.adapters.input.plain_diff import PlainDiffProvider
from code_review_agent.adapters.model.openai_compatible import (
    OpenAICompatibleProvider,
)
from code_review_agent.adapters.output.markdown import MarkdownOutputAdapter
from code_review_agent.adapters.publication.github import GitHubPublisher
from code_review_agent.adapters.publication.gitlab import GitLabPublisher
from code_review_agent.adapters.registry import InputRegistry, ModelRegistry
from code_review_agent.adapters.security.scanner import (
    FixedSecurityScanner,
    load_packaged_security_policy,
)
from code_review_agent.adapters.tools.registry import ToolRegistry
from code_review_agent.adapters.tools.runtime import RestrictedToolRuntime
from code_review_agent.application.dto import (
    BudgetReportView,
    PersistedTraceArtifactView,
    ReviewProgressView,
    ReviewRunResult,
    StartReviewCommand,
    TraceReportView,
)
from code_review_agent.application.orchestration import (
    ReviewDependencies,
    ReviewOrchestrator,
)
from code_review_agent.application.persistence import ReviewStateStore
from code_review_agent.application.publication_service import PublicationService
from code_review_agent.application.task_service import LocalDiffReviewService
from code_review_agent.config import CliConfig
from code_review_agent.domain.budget.models import UsageState as BudgetUsageState
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
from code_review_agent.domain.publication.models import PublicationResult
from code_review_agent.domain.publication.planner import PublicationPlanner
from code_review_agent.domain.report.builder import ReportBuilder
from code_review_agent.domain.report.models import ResultSnapshot, ResultSnapshotKind
from code_review_agent.domain.security.models import (
    ArtifactDescriptor,
    ArtifactKind,
    ArtifactPurpose,
    ArtifactSource,
    SecurityDecision,
    TrustLabel,
)
from code_review_agent.domain.security.service import SecurityService
from code_review_agent.domain.task.models import TaskSpec


class CliRuntime(Protocol):
    def review(
        self,
        command: StartReviewCommand,
        provider: str,
        model: str,
        request_id: str,
    ) -> ReviewRunResult: ...

    def status(self, task_id: str) -> ReviewProgressView: ...

    def trace(self, trace_id: str) -> tuple[object, ...]: ...

    def resume(
        self, task_id: str, *, confirm_unknown_retry: bool = False
    ) -> ReviewRunResult: ...

    def terminate(
        self, task_id: str, *, reason: str, expected_version: int
    ) -> ReviewRunResult: ...

    def cleanup(self, task_id: str, *, expected_version: int) -> None: ...

    def retry_delivery(
        self, task_id: str, *, expected_version: int
    ) -> ReviewRunResult: ...

    def providers(self) -> tuple[dict[str, str], ...]: ...

    def publish(self, task_id: str) -> PublicationResult: ...


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
    input_registry: InputRegistry[Any]
    trace_store: ReviewStateStore
    normalized: Any = None
    latest_snapshot: ResultSnapshot | None = None
    diff_text: str = ""
    trace_persistence_failed: bool = False

    def _trace(
        self,
        event_type: str,
        category: str,
        summary: dict[str, object],
        idempotency_key: str,
        artifact: PersistedTraceArtifactView | None = None,
    ) -> None:
        try:
            self.trace_store.append_trace_event(
                task_id=self.task_id,
                event_id=f"{self.task_id}:{idempotency_key}",
                event_type=event_type,
                category=category,
                summary=summary,
                idempotency_key=idempotency_key,
                artifact=artifact,
            )
        except Exception:
            self.trace_persistence_failed = True

    def _artifact(
        self, reference: Any, purpose: ArtifactPurpose
    ) -> PersistedTraceArtifactView | None:
        if reference is None:
            return None
        try:
            content = self.security.resolve(reference, expected_purpose=purpose)
        except Exception:
            self.trace_persistence_failed = True
            return None
        return PersistedTraceArtifactView(
            artifact_id=reference.artifact_id,
            purpose=reference.purpose.value,
            content=content,
            content_digest=reference.sanitized_digest,
            security_decision=reference.decision.value,
        )

    def normalize(self, command: StartReviewCommand) -> Any:
        if command.source_url is None:
            self.normalized = self.input_service.normalize_plain_diff(
                task_id=self.task_id,
                text=command.diff_text,
                file_path=command.diff_file,
            )
        else:
            provider_id = (
                "github"
                if command.source_url.startswith("https://github.com/")
                else "gitlab"
            )
            provider = self.input_registry.resolve_exact(provider_id, "1")
            self.normalized = InputService(
                provider, self.security, clock=SystemClock()
            ).normalize_remote(task_id=self.task_id, source_url=command.source_url)
        self.diff_text = self.security.resolve(
            self.normalized.change_set.sanitized_diff_ref,
            expected_purpose=ArtifactPurpose.DOMAIN_INGRESS,
        )
        self._trace(
            "input.normalized",
            "input",
            {"file_count": len(self.normalized.change_set.files)},
            "input-normalized",
        )
        return self.normalized

    def plan(self, normalized: Any) -> Any:
        tools = tuple(
            f"{tool.tool_id}@{tool.version}" for tool in self.tool_registry.catalog()
        )
        task_spec = TaskSpec(
            spec_id="config-spec-1",
            input_intent=normalized.change_set.identity.input_type,
            provider_id=self.config.provider_id,
            provider_version=self.config.provider_version,
            model_id=self.config.model_id,
            provider_origin=self.config.provider_origin,
            ruleset_id=self.config.ruleset_id,
            ruleset_version=self.config.ruleset_version,
            tools=tools,
            security_policy_id=self.config.security_policy_id,
            security_policy_version=self.config.security_policy_version,
            config_digest=sha256_bytes(b"code-review-agent.toml"),
            credential_alias=(
                "none"
                if normalized.change_set.identity.input_type == "plain_diff"
                else "shared"
            ),
            budget_account_id=self.budget_account_id,
        )
        capabilities = self.provider.capabilities
        plan = (
            ReviewPlanner()
            .plan(
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
                    tool_declarations=tuple(self.tool_registry.catalog()),
                )
            )
            .plan
        )
        self._trace(
            "planning.plan_created",
            "planning",
            {"plan_id": plan.plan_id, "work_unit_count": len(plan.work_units)},
            "plan-created",
        )
        return plan

    async def execute(self, unit: Any) -> WorkUnitExecutionResult:
        unit_diff = self._work_unit_diff(unit)
        result = await WorkUnitExecutor().execute(
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
                tool_runtime=RestrictedToolRuntime(self.tool_registry),
                system_rules="Review only the supplied change.",
                diff_text=unit_diff,
                model_call_guard=self.trace_store,
            )
        )
        for tool_attempt in result.tool_attempts:
            self._trace(
                (
                    "tool.succeeded"
                    if tool_attempt.state.value == "succeeded"
                    else "tool.failed"
                ),
                "tool",
                {
                    "tool_id": tool_attempt.tool_id,
                    "tool_version": tool_attempt.tool_version,
                    "work_unit_id": result.work_unit_id,
                    "state": tool_attempt.state.value,
                },
                f"{result.execution_id}-tool-{tool_attempt.tool_attempt_id}",
            )
        attempt = result.model_attempt
        if attempt is not None:
            terminal = f"model.call_{result.model_outcome_kind.value}"
            self._trace(
                terminal,
                "model",
                {
                    "model_call_id": attempt.model_call_id,
                    "work_unit_id": result.work_unit_id,
                },
                f"{result.execution_id}-model-terminal",
                self._artifact(
                    attempt.request_ref, ArtifactPurpose.TRACE_MODEL_REQUEST
                ),
            )
            provider_succeeded = (
                attempt.outcome is not None
                and
                attempt.outcome.state.provider_state is ProviderState.SUCCEEDED
            )
            accepted = provider_succeeded and result.error_code not in {
                "model_output_invalid",
                "security_boundary_failed",
            } and attempt.response_ref is not None
            if provider_succeeded:
                response_event = (
                    "model.response_accepted"
                    if accepted
                    else "model.response_rejected"
                )
                self._trace(
                    response_event,
                    "model",
                    {
                        "model_call_id": attempt.model_call_id,
                        "work_unit_id": result.work_unit_id,
                    },
                    f"{result.execution_id}-response",
                    self._artifact(
                        attempt.response_ref, ArtifactPurpose.TRACE_MODEL_RESPONSE
                    ),
                )
            summary = self.budget.get_summary(self.budget_account_id)
            self._trace(
                "budget.usage_settled",
                "budget",
                {
                    "known_consumption": summary.known_consumption,
                    "uncertain_consumption": summary.uncertain_consumption,
                    "work_unit_id": result.work_unit_id,
                },
                f"{result.execution_id}-budget",
            )
        return result

    def _work_unit_diff(self, unit: Any) -> str:
        if self.normalized is None:
            raise ValueError("work_unit_input_not_normalized")
        expected_artifact = self.normalized.change_set.sanitized_diff_ref
        lines: list[str] = []
        previous_end = -1
        for reference in sorted(unit.content_refs, key=lambda item: item.start):
            if (
                reference.artifact != expected_artifact
                or reference.start <= 0
                or reference.start < previous_end
                or reference.end > len(self.diff_text)
            ):
                raise ValueError("work_unit_content_ref_invalid")
            prefix = self.diff_text[reference.start - 1 : reference.start]
            content = self.diff_text[reference.start : reference.end]
            if (
                prefix not in {" ", "+", "-"}
                or sha256_bytes(content.encode("utf-8"))
                != reference.content_digest
            ):
                raise ValueError("work_unit_content_ref_invalid")
            lines.append(prefix + content)
            previous_end = reference.end
        if not lines:
            raise ValueError("work_unit_content_ref_invalid")
        return "\n".join(lines) + "\n"

    def checkpoint_binding_digest(self, command: StartReviewCommand) -> str:
        capabilities = self.provider.capabilities
        tool_snapshot = self.tool_registry.freeze()
        task_spec = {
            "provider_id": self.config.provider_id,
            "provider_version": self.config.provider_version,
            "model_id": self.config.model_id,
            "provider_origin": self.config.provider_origin,
            "ruleset_id": self.config.ruleset_id,
            "ruleset_version": self.config.ruleset_version,
            "config_digest": sha256_bytes(canonical_json({
                    "budget": command.budget_tokens,
                    "max_cost_per_review_cny": str(
                        self.config.max_cost_per_review_cny
                    ),
                    "price_per_million_tokens_cny": str(
                        self.config.price_per_million_tokens_cny
                    ),
                "provider": self.config.provider_id,
                "model": self.config.model_id,
            }).encode()),
        }
        return sha256_bytes(
            canonical_json(
                {
                    "task_spec": task_spec,
                    "provider": {
                        "version": capabilities.provider_version,
                        "origin": capabilities.origin,
                    },
                    "max_output_tokens": self.config.max_output_tokens,
                    "structured_strategy": self.config.structured_output,
                    "tools": [
                        {
                            "id": item.tool_id,
                            "version": item.version,
                            "declaration_digest": item.declaration_digest,
                        }
                        for item in tool_snapshot.tools
                    ],
                    "security_policy_digest": self.security.policy.policy_digest,
                    "security_policy_config": {
                        "id": self.config.security_policy_id,
                        "version": self.config.security_policy_version,
                    },
                }
            ).encode()
        )

    def consolidate(
        self, normalized: Any, plan: Any, executions: tuple[Any, ...]
    ) -> Any:
        finding_set = (
            FindingProcessor()
            .process(
                FindingProcessingRequest(
                    request_id=f"consolidate-{self.task_id}",
                    task_id=self.task_id,
                    execution_fact_boundary_id=f"boundary-{self.task_id}",
                    change_set=normalized.change_set,
                    plan=plan,
                    executions=executions,
                )
            )
            .finding_set
        )
        for finding in finding_set.findings:
            source_candidate_ids = set(finding.source_candidate_ids)
            work_unit_ids = tuple(
                sorted(
                    {
                        execution.work_unit_id
                        for execution in executions
                        if any(
                            candidate.candidate_id in source_candidate_ids
                            for candidate in execution.candidates
                        )
                    }
                )
            )
            self._trace(
                "finding.validated",
                "finding",
                {
                    "finding_id": finding.finding_id,
                    "trace_id": finding.trace_id,
                    "work_unit_id": work_unit_ids[0],
                },
                f"finding-{finding.finding_id}",
            )
            try:
                self.trace_store.link_finding_trace(
                    trace_id=finding.trace_id,
                    task_id=self.task_id,
                    finding_id=finding.finding_id,
                    work_unit_ids=work_unit_ids,
                )
            except Exception:
                self.trace_persistence_failed = True
        return finding_set

    def _trace_report(self) -> TraceReportView:
        try:
            events = self.trace_store.trace(self.task_id)
        except ValueError:
            events = ()
        types = tuple(event.event_type for event in events)
        return TraceReportView(
            task_id=self.task_id,
            event_count=len(events),
            model_call_count=sum(item.startswith("model.call_") for item in types),
            accepted_count=types.count("model.response_accepted"),
            rejected_count=types.count("model.response_rejected"),
            error_count=sum("failed" in item or "rejected" in item for item in types),
            query_command=f"uv run code-review-agent trace show {self.task_id}",
        )

    def _budget_report(self) -> BudgetReportView:
        summary = self.budget.get_summary(self.budget_account_id)
        cost = self.config.cost_cny_for_tokens
        return BudgetReportView(
            task_id=self.task_id,
            authorized=summary.authorized,
            known_consumption=summary.known_consumption,
            uncertain_consumption=summary.uncertain_consumption,
            active_reservations=summary.active_reservations,
            remaining_budget=summary.remaining_budget,
            overage=summary.overage,
            authorization_deficit=summary.authorization_deficit,
            account_state=summary.account_state.value,
            ledger_version=summary.ledger_version,
            currency="CNY",
            configured_cost_limit=format(
                self.config.max_cost_per_review_cny, "f"
            ),
            price_per_million_tokens=format(
                self.config.price_per_million_tokens_cny, "f"
            ),
            authorized_cost=format(cost(summary.authorized), "f"),
            known_cost=format(cost(summary.known_consumption), "f"),
            uncertain_cost=format(cost(summary.uncertain_consumption), "f"),
            active_reservations_cost=format(
                cost(summary.active_reservations), "f"
            ),
            remaining_cost=format(cost(summary.remaining_budget), "f"),
            overage_cost=format(cost(summary.overage), "f"),
            authorization_deficit_cost=format(
                cost(summary.authorization_deficit), "f"
            ),
        )

    def snapshot(
        self,
        command: StartReviewCommand,
        normalized: Any,
        plan: Any,
        executions: tuple[Any, ...],
        finding_set: Any,
    ) -> ResultSnapshot:
        if not normalized.change_set.files:
            result_state = "no_changes"
        else:
            coverage_states = {
                getattr(entry.state, "value", entry.state)
                for entry in finding_set.coverage.entries
            }
            result_state = (
                "unknown"
                if "unknown" in coverage_states
                else "partial"
                if coverage_states - {"reviewed"}
                else "complete_with_findings"
                if finding_set.findings
                else "complete_no_findings"
            )
        if self.trace_persistence_failed:
            result_state = "partial"
        snapshot = ResultSnapshot(
            snapshot_id=f"snapshot-{self.task_id}",
            task_id=self.task_id,
            snapshot_version=1,
            kind=ResultSnapshotKind.REVIEW,
            result_state=result_state,
            input_binding=normalized.binding,
            review_plan=plan,
            finding_set=finding_set,
            budget_summary=self._budget_report(),
            unknown_attempts=(),
            trace_summary=self._trace_report(),
            checkpoint_id=f"boundary-{self.task_id}",
            subject=command.subject,
            security_summary="fixed local security policy",
            failure_stage=(
                "trace_persistence_failed" if self.trace_persistence_failed else None
            ),
        )
        self.latest_snapshot = snapshot
        return snapshot

    def report(self, snapshot: ResultSnapshot) -> Any:
        self._trace(
            "report.generation_started",
            "report",
            {"snapshot_id": snapshot.snapshot_id},
            "report-generation-started",
        )
        if self.trace_persistence_failed and snapshot.result_state != "partial":
            snapshot = replace(
                snapshot,
                result_state="partial",
                failure_stage="trace_persistence_failed",
            )
        return ReportBuilder().build(snapshot)

    def deliver(self, model: Any, target: Path) -> Any:
        delivery = MarkdownOutputAdapter().deliver(model, target)
        self._trace(
            "report.delivered",
            "report",
            {"delivered": True},
            "report-delivered",
        )
        return delivery


class ConfiguredRuntime:
    def __init__(self, config: CliConfig) -> None:
        self.config = config
        self.orchestrator = ReviewOrchestrator()
        self.store = ReviewStateStore(config.state_database)
        self.tasks = LocalDiffReviewService(self.orchestrator, store=self.store)
        self.model_registry: ModelRegistry[Any] = ModelRegistry()
        self.model_registry.register(
            config.provider_id,
            config.provider_version,
            self._create_model_provider(),
        )
        self.model_registry.freeze()

    def review(
        self,
        command: StartReviewCommand,
        provider: str,
        model: str,
        request_id: str,
    ) -> ReviewRunResult:
        del request_id
        if provider != self.config.provider_id or model != self.config.model_id:
            raise ValueError("provider_or_model_not_configured")
        command = replace(
            command,
            provider=provider,
            model=model,
            budget_tokens=self.config.budget_tokens_per_review,
        )
        security = self._new_security_service()
        budget = self._new_budget_service()
        account = budget.create_account(
            command.task_id,
            command.budget_tokens,
            capability_ref="capability-1",
        )
        steps = self._build_steps(
            task_id=command.task_id,
            security=security,
            budget=budget,
            account_id=account.account_id,
        )
        result = asyncio.run(self.tasks.start(command, ReviewDependencies(steps)))
        if steps.trace_persistence_failed:
            result = replace(
                result,
                result_state="partial",
                limitations=(
                    result.limitations
                    if "trace_persistence_failed" in result.limitations
                    else tuple((*result.limitations, "trace_persistence_failed"))
                ),
            )
            self.store.save(result)
        if command.source_url is not None:
            if steps.normalized is None or steps.latest_snapshot is None:
                raise ValueError("publication_snapshot_not_ready")
            plan = PublicationPlanner().plan(
                command.source_url, steps.normalized, steps.latest_snapshot
            )
            publications = self._publication_service(security)
            publications.prepare(plan)
            if command.publish:
                result = replace(
                    result,
                    publication=publications.publish(
                        command.task_id, self._publisher(plan.target.platform)
                    ),
                )
                self.store.save(result)
        return result

    def status(self, task_id: str) -> ReviewProgressView:
        from code_review_agent.application.query_service import ReviewQueryService

        return ReviewQueryService(self.orchestrator, self.tasks).get_progress(task_id)

    def trace(self, trace_id: str) -> tuple[object, ...]:
        from code_review_agent.application.query_service import ReviewQueryService

        return ReviewQueryService(self.orchestrator, self.tasks).get_trace(trace_id)

    def resume(
        self, task_id: str, *, confirm_unknown_retry: bool = False
    ) -> ReviewRunResult:
        command, provider, model, budget_tokens = self.store.command(task_id)
        if provider != self.config.provider_id or model != self.config.model_id:
            raise ValueError("provider_or_model_not_configured")
        security = SecurityService(
            FixedSecurityScanner(), policy=load_packaged_security_policy()
        )
        budget = self._new_budget_service()
        account = budget.restore_account(
            task_id, budget_tokens, capability_ref="capability-1"
        )
        for reservation_id in self.store.recover_pending_model_calls(task_id):
            try:
                budget.settle_uncertain_usage(
                    reservation_id,
                    usage_state=BudgetUsageState.MISSING,
                    reason="model call started without a persisted terminal outcome",
                )
            except ValueError as exc:
                if "already settled" not in str(exc):
                    raise
        steps = self._build_steps(
            task_id=task_id,
            security=security,
            budget=budget,
            account_id=account.account_id,
        )
        return asyncio.run(
            self.tasks.resume(
                task_id,
                ReviewDependencies(steps),
                confirm_unknown_retry=confirm_unknown_retry,
            )
        )

    def _build_steps(
        self,
        *,
        task_id: str,
        security: SecurityService,
        budget: BudgetService,
        account_id: str,
    ) -> _ConfiguredSteps:
        model_provider = self.model_registry.resolve_exact(
            self.config.provider_id, self.config.provider_version
        )
        return _ConfiguredSteps(
            config=self.config,
            task_id=task_id,
            security=security,
            input_service=InputService(
                PlainDiffProvider(security), security, clock=SystemClock()
            ),
            budget=budget,
            budget_account_id=account_id,
            provider=model_provider,
            tool_registry=ToolRegistry.from_builtin(),
            input_registry=_build_input_registry(security),
            trace_store=self.store,
        )

    @staticmethod
    def _new_security_service() -> SecurityService:
        return SecurityService(
            FixedSecurityScanner(), policy=load_packaged_security_policy()
        )

    def _new_budget_service(self) -> BudgetService:
        return BudgetService(self.store)

    def terminate(
        self, task_id: str, *, reason: str, expected_version: int
    ) -> ReviewRunResult:
        self.tasks.get(task_id)
        if expected_version != 1:
            raise ValueError("version_conflict")
        return self.tasks.terminate(task_id, reason=reason)

    def cleanup(self, task_id: str, *, expected_version: int) -> None:
        if expected_version != 1:
            raise ValueError("version_conflict")
        self.tasks.cleanup(task_id)

    def retry_delivery(self, task_id: str, *, expected_version: int) -> ReviewRunResult:
        result = self.tasks.get(task_id)
        if expected_version != 1:
            raise ValueError("version_conflict")
        if result.report_path is None:
            raise ValueError("report_not_found")
        if not result.report_path.exists():
            raise ValueError("report_not_found")
        return result

    def providers(self) -> tuple[dict[str, str], ...]:
        return (
            {"kind": "input", "provider_id": "plain_diff", "version": "1"},
            {"kind": "input", "provider_id": "github", "version": "1"},
            {"kind": "input", "provider_id": "gitlab", "version": "1"},
            *(
                {
                    "kind": "model",
                    "provider_id": provider_id,
                    "version": version,
                }
                for provider_id, version in self.model_registry.list()
            ),
        )

    def publish(self, task_id: str) -> PublicationResult:
        publications = self._publication_service(self._new_security_service())
        try:
            plan = self.store.publication_plan(task_id)
        except ValueError as exc:
            if str(exc) != "publication_not_found":
                raise
            command, _, _, _ = self.store.command(task_id)
            raise ValueError(
                "publication_not_remote_input"
                if command.source_url is None
                else "publication_snapshot_not_ready"
            ) from None
        result = publications.publish(task_id, self._publisher(plan.target.platform))
        review = self.tasks.get(task_id)
        self.store.save(replace(review, publication=result))
        return result

    def _publisher(self, platform: str) -> Any:
        credentials = KeyringCredentialStore()
        if platform == "github":
            return GitHubPublisher(credentials)
        if platform == "gitlab":
            return GitLabPublisher(credentials)
        raise ValueError("publication_target_invalid")

    def _publication_service(self, security: SecurityService) -> PublicationService:
        def scan(body: str, task_id: str) -> str:
            descriptor = ArtifactDescriptor(
                artifact_id="publication-" + sha256_bytes(body.encode()),
                task_id=task_id,
                source=ArtifactSource.TRUSTED_APPLICATION,
                trust_label=TrustLabel.UNTRUSTED_TEXT,
                kind=ArtifactKind.REPORT_PAYLOAD,
                purpose=ArtifactPurpose.REPORT_DELIVERY,
                provenance=("remote-publication",),
                max_size=262_144,
            )
            prepared = security.evaluate_artifact(body, descriptor)
            if prepared.decision not in (
                SecurityDecision.SAFE,
                SecurityDecision.REDACTED,
            ):
                raise ValueError("publication_content_rejected")
            return prepared.sanitized_payload

        return PublicationService(self.store, scan=scan)

    def _create_model_provider(self) -> Any:
        if self.config.provider_id == "openai-compatible":
            return OpenAICompatibleProvider(self.config)
        return LocalModelProvider(self.config)


def build_runtime(config: CliConfig | None = None) -> CliRuntime:
    selected = config or CliConfig.from_file(Path("code-review-agent.toml"))
    return ConfiguredRuntime(selected)


def _build_input_registry(security: SecurityService) -> InputRegistry[Any]:
    credentials = KeyringCredentialStore()
    registry: InputRegistry[Any] = InputRegistry()
    registry.register("github", "1", GitHubInputProvider(security, credentials))
    registry.register("gitlab", "1", GitLabInputProvider(security, credentials))
    registry.freeze()
    return registry
