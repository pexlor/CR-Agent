# Trace 持久化与 Budget/Trace 展示设计

## 背景

当前 CLI 的 `ReviewStateStore` 只把七条编排阶段事件序列化进
`review_runs.payload`。`trace show` 因此只能展示阶段切换，无法追溯实际模型请求、
模型回复、预算结算、响应验证和 Finding 生成过程。现有 `SecurityService` 保存的
脱敏 artifact 只存在于进程内存，进程退出后无法解析。

Markdown 报告还直接渲染 `BudgetSummary(...)` 和 `namespace(...)` 的 Python
对象表示，Location 行的 Markdown 反引号也不正确。这些行为不满足可恢复、可追溯
和面向用户的报告交付要求。

## 目标

1. 将审查运行中的关键 Trace 事件和脱敏 artifact 追加写入 SQLite。
2. 进程重启后，`trace show <task-id>` 仍能读取完整脱敏 Trace。
3. 模型请求、模型回复、预算结算、错误和 Finding 能沿同一任务时间线核对。
4. 报告只展示紧凑 Trace 摘要，完整内容通过 CLI 查询。
5. Budget、Trace 和 Location 使用稳定、可读的 Markdown，而不是 Python `repr`。
6. 详细 Trace 默认保留七天，现有 cleanup 删除详细 Trace，并保留报告和墓碑。

## 非目标

- 不把当前 CLI 全面迁移到 `0001_initial.sql` 描述的完整持久化体系。
- 不持久化 Authorization header、API Key、原始未脱敏 secret 或模型内部
  `reasoning_content`。
- 不修改模型重试、预算计费、恢复或远程平台认证语义。
- 不在 Markdown 报告中嵌入完整 prompt 或完整模型回复。
- 不增加网络服务、后台清理进程或多用户访问控制。

## 方案选择

采用扩展当前 `ReviewStateStore` 的方案。它使用当前 CLI 已经依赖的 SQLite 文件，
新增专用追加式表，并复用已有 Trace 类别和安全 artifact purpose。完整基础设施迁移
需要同时替换 task、checkpoint、budget 和 delivery 存储，不属于本次增量修复。

### ADR：选择当前 CLI Store 的追加式扩展

- **状态：** 已接受。
- **决定：** 新建 `review_trace_events` 和 `review_trace_artifacts` 表，不把 Trace
  继续嵌入 `review_runs.payload`。
- **理由：** 独立表支持顺序、幂等、过期清理、重启查询和 artifact 大小隔离；同时
  避免一次性迁移全部未接入的领域持久化表。
- **代价：** 当前 CLI 表与长期完整 schema 暂时并存。表名使用 `review_trace_*`，避免
  与未来 `trace_events` 迁移冲突。

## 数据模型

### review_trace_artifacts

| 字段 | 约束与含义 |
| --- | --- |
| `artifact_id` | 主键；稳定标识一次脱敏内容 |
| `task_id` | 非空；所属审查任务 |
| `purpose` | 非空；例如 `trace_model_request` |
| `content` | 非空；安全扫描通过或已脱敏的 UTF-8 正文 |
| `content_digest` | 64 位 SHA-256，与正文重新计算结果一致 |
| `security_decision` | `safe` 或 `redacted` |
| `created_at` | UTC ISO-8601 时间 |
| `expires_at` | UTC ISO-8601 时间，默认创建时间后七天 |

### review_trace_events

| 字段 | 约束与含义 |
| --- | --- |
| `event_id` | 主键 |
| `task_id` | 非空；所属审查任务 |
| `sequence` | 任务内从 1 开始连续递增 |
| `event_type` | 现有标准事件名，如 `model.call_succeeded` |
| `category` | `input`、`planning`、`model`、`budget`、`finding`、`report` 等 |
| `summary_json` | 只允许无敏感正文的标量摘要 |
| `artifact_id` | 可空；关联一个脱敏 artifact |
| `idempotency_key` | 任务内唯一，避免恢复时重复写入 |
| `created_at` | UTC ISO-8601 时间 |
| `expires_at` | 与任务 Trace 保留期一致 |

唯一约束为 `(task_id, sequence)` 和 `(task_id, idempotency_key)`。写入事件与其 artifact
必须位于同一个 SQLite 事务中；失败时不得只留下半条引用。

## 安全边界

### 请求

`PreparedModelRequest.body` 不包含 Authorization header。发送前继续执行现有
`MODEL_EGRESS` 检查；随后使用 `TRACE_MODEL_REQUEST` purpose 再次评估正文。只有
`safe` 或 `redacted` 结果才能 commit，并写入 Trace artifact。

### 回复

只记录 Provider 已解析的 `response_payload`，不保存 HTTP 原始 body，因此不会包含
千帆返回的 `reasoning_content`。payload 使用 canonical JSON 编码后，以
`TRACE_MODEL_RESPONSE` purpose 执行扫描、脱敏和 commit。

### 失败语义

- 模型出站安全检查失败：保持现有 fail-closed 行为，不发送请求。
- Trace artifact 扫描或持久化失败：本次工作单元不得声称 fully reviewed；返回
  `trace_persistence_failed` 并生成 partial 报告。
- API Key 只存在于 Provider 发送时构造的 header，不进入 PreparedModelRequest、
  Trace、日志或报告。
- Trace 摘要禁止使用 `content`、`raw`、`response`、`reasoning` 等字段名。

## 运行时数据流

1. Runtime 创建任务级 Trace recorder，并把它注入 `_ConfiguredSteps`。
2. 输入规范化后记录 `input.normalized`，关联已脱敏 diff artifact。
3. 计划冻结后记录 `planning.plan_created`。
4. WorkUnitExecutor 生成并 commit 脱敏请求、回复引用，填充
   `ModelCallAttempt.request_ref` 和 `response_ref`。
5. `_ConfiguredSteps.execute` 解析引用并追加模型调用、响应接受或拒绝、预算结算事件。
6. Finding 汇总后，为每条最终 Finding 记录 `finding.validated`，summary 含 finding ID
   和 trace ID，不含正文。
7. 报告生成和交付分别追加 `report.generation_started` 与 `report.delivered`。
8. Snapshot 使用持久化 Store 生成的 `TraceReportView`，而不是
   `SimpleNamespace(task_id=...)`。

## 应用 DTO 与查询

新增不可变 DTO：

- `PersistedTraceEventView`：sequence、event type、category、summary、artifact metadata
  和可选脱敏正文。
- `TraceReportView`：task ID、总事件数、模型调用数、接受数、拒绝数、错误数和查询命令。
- `BudgetReportView`：授权、确定消耗、不确定消耗、剩余、超额、账户状态和账本版本。

`ReviewStateStore.trace(task_id)` 从新表按 sequence 查询。`ReviewQueryService.get_trace`
优先读取持久化 Trace；仅对没有持久化记录的旧任务回退到原阶段 Trace，保证向后兼容。

JSON 输出包含结构化 summary 和脱敏 artifact 内容。人类可读输出按事件分行，并在有关联
artifact 时缩进显示 purpose、digest 和正文。

## Markdown 展示

### Budget

使用表格展示：

| Metric | Tokens / Value |
| --- | --- |
| Authorized | 数值 |
| Known consumption | 数值 |
| Uncertain consumption | 数值 |
| Remaining | 数值 |
| Overage | 数值 |
| Account state | 状态 |
| Ledger version | 数值 |

### Trace

使用紧凑摘要表，包含事件数、模型调用数、接受数、拒绝数和错误数，并显示：

```bash
uv run code-review-agent trace show <task-id>
```

### Location

Location 必须渲染成一个合法代码跨度，例如 `file file_<digest>`；不再产生嵌套、未闭合
的反引号。

## 保留和清理

- 新 artifact/event 写入时设置七天到期时间。
- `ReviewStateStore` 初始化时删除已过期 Trace，先删 events 后删 artifacts。
- `cleanup <task-id>` 删除该任务的 Trace events 和 artifacts，同时执行现有运行详情清理；
  Markdown 报告和 tombstone 保持现有语义。
- 过期或 cleanup 后执行 `trace show` 返回 `trace_not_found`，不回显已删除内容。

## 测试策略

按 TDD 增加以下覆盖：

1. SQLite Trace 事件和 artifact 原子写入、连续 sequence、幂等键、重启读取。
2. WorkUnitExecutor 生成 request/response refs，且引用可从同一个 SecurityService 解析。
3. 模拟 secret 出现在 diff 和模型回显时，数据库、报告和 CLI 输出均无明文。
4. Trace 写入失败导致 partial 和 `trace_persistence_failed`，不伪装完整成功。
5. `trace show` JSON 和人类输出展示完整脱敏事件。
6. cleanup 和七天过期清理删除详细 Trace。
7. Budget、Trace、Location Markdown 使用固定快照断言，不包含 `BudgetSummary(` 或
   `namespace(`。
8. 进程重启后按 task ID 查询 Trace。
9. 全量 Ruff、mypy、pytest、acceptance 与真实千帆端到端复验。

## 风险与缓解

- **Trace 使数据库增长：** artifact 与事件独立存储，并设置七天到期清理。
- **模型回显 secret：** artifact 入库前强制经过专用 purpose 的完整扫描和脱敏。
- **恢复产生重复事件：** 使用任务内 idempotency key 唯一约束。
- **旧任务没有新 Trace：** 查询层保留阶段事件回退，不修改历史数据。
- **Trace 故障掩盖审查状态：** 写入失败明确降级，报告保留稳定错误码。
