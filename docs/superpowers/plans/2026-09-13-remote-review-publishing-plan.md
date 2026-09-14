# Remote PR/MR Review Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish complete CR-Agent review results as GitHub/GitLab line comments plus a summary with explicit authorization and recoverable idempotency.

**Architecture:** Keep remote writes in a new publication domain and application service, separate from read-only input and local Markdown output. Persist a deterministic publication plan before network I/O, reconcile hidden markers before writes, and use platform adapters for exact API contracts.

**Tech Stack:** Python 3.12, dataclasses, Typer, httpx, SQLite, pytest, respx.

**Spec:** `docs/superpowers/specs/2026-09-13-remote-review-publishing-design.md`

## Global Constraints

- GitHub.com and GitLab.com only; redirects and environment proxies remain disabled.
- No remote write occurs without `review --publish` or `publish --confirm`.
- Local Markdown delivery and remote publication retain separate states.
- Every remote body is security-scanned and contains a stable hidden marker.
- Transport/5xx ambiguity is persisted as unknown and reconciled before retry.
- Existing user files and unrelated worktree changes must not be modified.

---

### Task 1: Publication domain, renderer, and deterministic planner

**Files:**
- Create: `src/code_review_agent/domain/publication/__init__.py`
- Create: `src/code_review_agent/domain/publication/models.py`
- Create: `src/code_review_agent/domain/publication/planner.py`
- Test: `tests/unit/domain/publication/test_planner.py`

**Interfaces:**
- Consumes: `ResultSnapshot`, `NormalizedInput`, `FinalFinding`, and source URL.
- Produces: `PublicationPlan`, ordered `PublicationItem` values, `PublicationResult`, and stable marker bodies.

- [ ] **Step 1: Write failing planner tests**

```python
def test_planner_maps_new_and_old_lines_and_keeps_file_findings_in_summary():
    plan = PublicationPlanner().plan(source_url, normalized, snapshot)
    assert [item.kind for item in plan.items] == ["line", "line", "summary"]
    assert plan.items[0].position.side == "RIGHT"
    assert plan.items[1].position.side == "LEFT"
    assert "cr-agent:publication:" in plan.items[-1].body

def test_publication_keys_are_stable_and_snapshot_bound():
    first = PublicationPlanner().plan(source_url, normalized, snapshot)
    second = PublicationPlanner().plan(source_url, normalized, snapshot)
    assert first == second
```

- [ ] **Step 2: Run tests and observe imports/behavior fail because publication domain is absent**

Run: `uv run pytest tests/unit/domain/publication/test_planner.py -q`

- [ ] **Step 3: Implement immutable models, rendering, URL parsing, path lookup, line validation, keys, and aggregate-state calculation**

```python
class PublicationState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"

class PublicationPlanner:
    def plan(self, source_url: str, normalized: NormalizedInput,
             snapshot: ResultSnapshot) -> PublicationPlan:
        return PublicationPlan.from_review(source_url, normalized, snapshot)
```

- [ ] **Step 4: Run planner tests until green**

Run: `uv run pytest tests/unit/domain/publication/test_planner.py -q`

### Task 2: GitHub and GitLab publisher contracts

**Files:**
- Create: `src/code_review_agent/ports/publication.py`
- Create: `src/code_review_agent/adapters/publication/__init__.py`
- Create: `src/code_review_agent/adapters/publication/base.py`
- Create: `src/code_review_agent/adapters/publication/github.py`
- Create: `src/code_review_agent/adapters/publication/gitlab.py`
- Modify: `src/code_review_agent/adapters/input/remote.py`
- Modify: `src/code_review_agent/adapters/input/gitlab.py`
- Modify: `src/code_review_agent/domain/input/models.py`
- Modify: `src/code_review_agent/domain/input/service.py`
- Modify: `src/code_review_agent/domain/task/models.py`
- Test: `tests/contract/test_remote_publishers.py`
- Test: `tests/contract/test_remote_input_providers.py`

**Interfaces:**
- Consumes: `PublicationTarget`, `PublicationPosition`, body, marker, and shared credential store.
- Produces: verified `RemoteTargetState`, marker-to-remote-comment mappings, and `RemoteComment` create results; raises `PublicationError(code, outcome_unknown)`.

- [ ] **Step 1: Write failing exact-request contract tests**

```python
def test_github_posts_right_side_line_and_summary(respx_mock):
    # GET metadata/list endpoints, POST /pulls/7/comments and /issues/7/comments.
    # Assert the literal JSON payloads and returned identifiers.

def test_gitlab_posts_old_line_discussion_with_all_diff_refs(respx_mock):
    # Assert position_type, base_sha, start_sha, head_sha, paths, old_line.

def test_post_transport_error_is_unknown():
    # Assert PublicationError.outcome_unknown is True.
```

- [ ] **Step 2: Run contract tests and observe missing adapters fail**

Run: `uv run pytest tests/contract/test_remote_publishers.py tests/contract/test_remote_input_providers.py -q`

- [ ] **Step 3: Retain GitLab start SHA in remote input identity and implement guarded publisher adapters**

```python
class RemotePublisherPort(Protocol):
    def verify_target(self, target: PublicationTarget) -> RemoteTargetState:
        raise NotImplementedError
    def find_markers(self, target: PublicationTarget,
                     markers: tuple[str, ...]) -> dict[str, RemoteComment]:
        raise NotImplementedError
    def create_line_comment(self, target: PublicationTarget,
                            item: PublicationItem) -> RemoteComment:
        raise NotImplementedError
    def create_summary_comment(self, target: PublicationTarget,
                               item: PublicationItem) -> RemoteComment:
        raise NotImplementedError
```

- [ ] **Step 4: Run publisher and input contracts until green**

Run: `uv run pytest tests/contract/test_remote_publishers.py tests/contract/test_remote_input_providers.py -q`

### Task 3: Persistent publication orchestration and reconciliation

**Files:**
- Create: `src/code_review_agent/application/publication_service.py`
- Modify: `src/code_review_agent/application/persistence.py`
- Create: `src/code_review_agent/migrations/0002_remote_publications.sql`
- Test: `tests/integration/test_remote_publication.py`
- Test: `tests/integration/test_sqlite_infrastructure.py`

**Interfaces:**
- Consumes: a deterministic `PublicationPlan`, a `RemotePublisherPort`, security scan callback, and `ReviewStateStore`.
- Produces: persisted `PublicationResult`; methods `prepare(plan)`, `publish(task_id, publisher)`, and `get(task_id)`.

- [ ] **Step 1: Write failing persistence and recovery tests**

```python
def test_partial_publication_resumes_without_duplicate_remote_writes(tmp_path):
    service.prepare(plan)
    first = service.publish(plan.task_id, failing_after_one)
    assert first.state == "partial"
    second = service.publish(plan.task_id, succeeding_publisher)
    assert second.state == "succeeded"
    assert succeeding_publisher.created_keys == (remaining_key, summary_key)

def test_unknown_write_is_reconciled_by_marker_before_retry(tmp_path):
    service.prepare(plan)
    assert service.publish(plan.task_id, response_lost).state == "unknown"
    assert service.publish(plan.task_id, marker_found).state == "succeeded"
    assert marker_found.created_keys == ()
```

- [ ] **Step 2: Run integration tests and observe missing service/storage fail**

Run: `uv run pytest tests/integration/test_remote_publication.py tests/integration/test_sqlite_infrastructure.py -q`

- [ ] **Step 3: Implement SQLite rows, transactions, lease acquisition, state transitions, reconciliation, trace events, and cleanup**

```python
class PublicationService:
    def prepare(self, plan: PublicationPlan) -> PublicationResult:
        self.store.save_publication_plan(plan)
        return self.store.publication_result(plan.task_id)
    def publish(self, task_id: str,
                publisher: RemotePublisherPort) -> PublicationResult:
        plan = self.store.publication_plan(task_id)
        return self._publish_plan(plan, publisher)
```

- [ ] **Step 4: Run persistence/recovery tests until green**

Run: `uv run pytest tests/integration/test_remote_publication.py tests/integration/test_sqlite_infrastructure.py -q`

### Task 4: Runtime and CLI integration

**Files:**
- Modify: `src/code_review_agent/application/dto.py`
- Modify: `src/code_review_agent/application/orchestration.py`
- Modify: `src/code_review_agent/application/task_service.py`
- Modify: `src/code_review_agent/bootstrap.py`
- Modify: `src/code_review_agent/cli/commands.py`
- Modify: `src/code_review_agent/cli/presenters.py`
- Modify: `src/code_review_agent/cli/exit_codes.py`
- Test: `tests/integration/cli/test_cli_commands.py`
- Test: `tests/integration/test_plain_diff_review_flow.py`

**Interfaces:**
- `StartReviewCommand.publish: bool = False`.
- `ReviewRunResult.publication: PublicationResult | None = None`.
- `CliRuntime.publish(task_id: str) -> PublicationResult`.

- [ ] **Step 1: Write failing CLI tests for explicit authorization and separated result state**

```python
def test_review_publish_invokes_remote_publication_once(runner):
    result = runner.invoke(app, ["review", "--url", PR, "--provider", P,
                                 "--model", M, "--publish", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["publication"]["state"] == "succeeded"

def test_publish_requires_confirm(runner):
    result = runner.invoke(app, ["publish", "task-1"])
    assert result.exit_code == 2
```

- [ ] **Step 2: Run CLI and flow tests and observe new flags/commands fail**

Run: `uv run pytest tests/integration/cli/test_cli_commands.py tests/integration/test_plain_diff_review_flow.py -q`

- [ ] **Step 3: Wire publication planning after snapshot creation, publisher selection, CLI commands, result presentation, and non-zero publication failures**

```python
@app.command()
def publish(task_id: str,
            confirm: bool = typer.Option(False, "--confirm"),
            json_output: bool = typer.Option(False, "--json")) -> None:
    if not confirm:
        raise typer.Exit(USAGE_ERROR)
    result = runtime_factory().publish(task_id)
```

- [ ] **Step 4: Run CLI and review-flow tests until green**

Run: `uv run pytest tests/integration/cli/test_cli_commands.py tests/integration/test_plain_diff_review_flow.py -q`

### Task 5: Documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `docs/已知限制.md`
- Modify: `config.example.toml` only if a new bounded publication setting is required.

**Interfaces:**
- Documents exact credential scopes, commands, safety behavior, states, and unsupported hosts.

- [ ] **Step 1: Update operator documentation after behavior is covered by tests**

```text
review --url https://github.com/owner/repository/pull/123 --publish
publish <task-id> --confirm
```

- [ ] **Step 2: Run formatting/static checks configured by the project**

Run: `uv run ruff check . && uv run mypy src` when available in the project environment.

- [ ] **Step 3: Run the full suite and inspect the working diff**

Run: `uv run pytest -q && git diff --check && git status --short`

- [ ] **Step 4: Commit implementation without staging unrelated user files**

```bash
git add README.md docs/ src/ tests/
git commit -m "feat: publish reviews to GitHub and GitLab"
```
