# Trace 持久化与报告展示实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框跟踪进度。

**目标：** 将已脱敏的模型请求、回复、预算和 Finding 事件持久化到 SQLite，并用结构化 Budget/Trace 视图生成可读报告。

**架构：** 当前 CLI `ReviewStateStore` 新增追加式 Trace event/artifact 表；执行器填充已有 `ModelCallAttempt` 安全引用；运行时把引用解析为已脱敏正文后持久化。报告使用专用 presentation DTO，完整 Trace 由 `trace show` 查询。

**技术栈：** Python 3.12、SQLite、pytest、Typer、Jinja2、Ruff、mypy。

---

### 任务 1：SQLite Trace Store

**文件：**
- 修改：`src/code_review_agent/application/dto.py`
- 修改：`src/code_review_agent/application/persistence.py`
- 修改：`src/code_review_agent/application/task_service.py`
- 测试：`tests/integration/recovery/test_persistent_recovery.py`

- [ ] 编写失败测试：追加 artifact/event 后新 Store 实例能读取连续事件；重复 idempotency key 不重复；cleanup 删除详情。
- [ ] 运行测试确认因 API/表不存在而失败。
- [ ] 新增 `TraceArtifactView`、`PersistedTraceEventView`，创建 `review_trace_artifacts`、`review_trace_events` 表并实现事务写入、查询、过期清理和 cleanup。
- [ ] 运行测试确认通过。

### 任务 2：执行器安全 Trace 引用

**文件：**
- 修改：`src/code_review_agent/domain/execution/executor.py`
- 测试：`tests/unit/domain/execution/test_executor.py`

- [ ] 编写失败测试：成功调用必须携带 `TRACE_MODEL_REQUEST` 和 `TRACE_MODEL_RESPONSE` 引用，引用内容不含模拟 secret。
- [ ] 运行测试确认引用当前为 `None`。
- [ ] 复用 `SecurityService` 对请求/回复执行 Trace purpose 扫描、commit，并填充 `ModelCallAttempt`；扫描不确定时返回 `trace_persistence_failed`。
- [ ] 运行执行器测试确认通过。

### 任务 3：运行时事件接线与重启查询

**文件：**
- 修改：`src/code_review_agent/bootstrap.py`
- 修改：`src/code_review_agent/application/query_service.py`
- 修改：`src/code_review_agent/cli/presenters.py`
- 测试：`tests/integration/model/test_openai_provider_review.py`
- 测试：`tests/integration/cli/test_cli_commands.py`

- [ ] 编写失败测试：一次 mock 模型审查后新 runtime 能查询 input、planning、model request/response、budget、finding 事件及脱敏正文。
- [ ] 运行测试确认只返回阶段事件。
- [ ] `_ConfiguredSteps` 在各阶段追加事件；QueryService 优先读取持久化事件；human/JSON presenter 输出结构化事件和 artifact。
- [ ] 运行集成测试确认通过。

### 任务 4：Budget/Trace/Location 报告展示

**文件：**
- 修改：`src/code_review_agent/domain/report/models.py`
- 修改：`src/code_review_agent/domain/report/builder.py`
- 修改：`src/code_review_agent/resources/templates/review.md.j2`
- 测试：`tests/unit/domain/report/test_builder.py`
- 测试：`tests/integration/test_markdown_delivery.py`

- [ ] 编写失败测试：报告不得包含 `BudgetSummary(` 或 `namespace(`，必须包含 Budget 表、Trace 摘要、查询命令和合法 Location。
- [ ] 运行测试确认当前模板失败。
- [ ] 新增 `BudgetReportView`、`TraceReportView`，由 builder 映射领域摘要；更新模板为稳定表格和合法代码跨度。
- [ ] 运行报告测试确认通过。

### 任务 5：全面和真实复验

- [ ] 运行 Ruff、mypy、全量 pytest、acceptance 和变更文件格式检查。
- [ ] 合并到 `main` 后使用本地 `0600` 千帆配置执行真实 Git diff 审查。
- [ ] 新进程执行 `trace show <task-id>`，核对请求、回复、预算和 Finding 事件。
- [ ] 扫描报告、状态库和 evidence，确认模型 API Key 与模拟 secret 均未明文泄漏。
- [ ] 清理隔离 worktree 和已合并分支。
