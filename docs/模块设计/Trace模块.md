# Code Review Agent Trace 模块详细设计

## 1. 文档状态

- 日期：2026-09-10
- 状态：设计完成，待复审
- 适用范围：本地 CLI、单进程、单机 SQLite、同一可信 OS 账号
- 主要需求：FR-015、FR-016、FR-026

本文定义 Trace 的事实事件、因果关系、逐评论关联、事务边界、查询、保留和清理语义。SQLite 物理表、索引和 migration SQL 由基础设施模块设计，但不得改变本文不变量。

## 2. 设计结论

Trace 模块采用“追加写事实事件 + 显式因果边 + 逐评论 `TraceLink` + 查询时投影”方案：

1. Trace 只记录实际发生且已安全处理的事实，不驱动工作流；
2. 所有事件使用统一信封，payload 按事件类型和版本解释；
3. 因果关系由产生事实的业务操作显式声明，不按时间自动推断；
4. 每条最终评论有独立 `TraceLink`，关联其真实输入、模型、工具和验证依据；
5. payload 只能是用途匹配的 `SanitizedArtifactRef` 或白名单结构；
6. 事件提交后不可原地修改，更正通过追加事件表达；
7. Trace 与权威业务状态在同一 `PersistenceUnitOfWork` 中提交；
8. 完整 Trace 仅供本机可信用户查看，与 checkpoint 使用统一生命周期；
9. 不记录或推测模型内部思维，不虚构未执行的工具或验证。

不采用普通 JSON 日志，因为其无法保证事务一致、逐评论追溯和原子清理；不采用完整事件溯源，因为任务、预算和调用已有各自权威状态，Trace 不应成为控制事实源。

## 3. 目标与非目标

### 3.1 目标

- 每条评论能独立追溯到相符的实际生成、证据和验证过程；
- 稳定关联任务、工作单元、尝试、模型/工具调用、预算、checkpoint、发现、评论和报告；
- 准确区分准备、开始、成功、失败、unknown、跳过和安全阻断；
- 区分来源材料、安全处理产物和实际发送模型的请求；
- 共享调用可被多条评论引用，但各评论直接证据仍可区分；
- Trace 与业务状态事务一致，重放和恢复不制造重复事实；
- 查询明确区分完整、受限、过期、已清理、清理失败和不存在。

### 3.2 非目标

- 不推进任务、计算预算、执行模型/工具或决定发现有效性；
- 不创建安全证明，不保存第三方 SDK 对象或原始异常；
- 不保存模型 chain-of-thought、reasoning 或推测出的内部思维；
- 不通过 Trace 重放模型或工具，不承诺重放得到逐字相同结果；
- 不替代调试日志、安全日志、checkpoint 或任务状态；
- 不提供远程 Trace 服务、跨用户权限和遥测上报。

## 4. 模块边界

Trace 模块拥有事件信封、任务内序号、事件边、逐评论 `TraceLink`、追加/更正规则、查询投影、完整性判定、幂等和清理参与规则。

任务、工作单元、调用、预算、安全证明、checkpoint、发现、结果快照和报告由各自模块拥有。Trace 只保存稳定标识、安全摘要和实际关系，不复制其可变权威状态。

其他模块不得直接插入 Trace 表，只能产生 `TraceMutationIntent`，由统一工作单元验证并提交。产生业务事实的用例负责在其权威状态事务中同时提交 Trace，禁止事后补写来伪装一致。

## 5. 核心概念

### 5.1 `TraceEvent`

表示一个已经发生并成功提交的事实。它必须与事件类型一致，只引用已提交或同事务提交的安全产物，并与对应业务状态原子提交。

### 5.2 `TraceEdge`

显式连接两个事件或业务对象。边分为因果边和非因果关联边，不因同任务、时间接近或文本相似而自动产生。

### 5.3 `TraceLink`

最终评论到其直接和共享依据的不可变关联记录。报告展示的 `trace_id` 对应 `TraceLink.trace_id`，不是任务 ID、调用 ID或单个事件 ID。

### 5.4 `TraceProjection`

从追加记录和权威业务状态生成的只读视图。投影不是事实源，不得反向修改任务、调用或预算。

## 6. 事件信封

每个事件使用统一 `TraceEventEnvelope`：

| 字段 | 约束 |
| --- | --- |
| `event_id` | 全局唯一，创建后不变 |
| `task_id` | 必填，不可跨任务改绑 |
| `sequence` | 任务内从 1 严格递增 |
| `event_type` | 固定小写点分名称 |
| `event_version` | 正整数 schema 版本 |
| `category` | 固定业务分类 |
| `fact_kind` | `observation / decision / transition / correction` |
| `occurred_at` | 事实发生 UTC 时间 |
| `recorded_at` | 事务提交 UTC 时间 |
| `producer` | 固定模块标识 |
| `correlation_id` | 任务或子过程关联组 |
| `causation_event_id` | 直接原因事件，可空且必须同任务 |
| `work_unit_id / attempt_id` | 适用时填写 |
| `model_call_id / tool_call_id` | 对应调用事件填写 |
| `checkpoint_id` | 对应 checkpoint 时填写 |
| `payload_ref` | 用途匹配的 `SanitizedArtifactRef`，可空 |
| `summary` | 白名单结构，不接受自由文本 |
| `idempotency_key` | 任务内唯一 |
| `retention_group_id` | 与任务详细数据保留组一致 |
| `schema_digest` | 防止同版本含义漂移 |

查询按 `sequence` 排序，不按第三方时间排序。时间不能代替因果关系。任务内序号在工作单元提交时连续分配；相同幂等键重放返回原事件和原序号。

## 7. 事件分类与标准类型

固定分类包括 `task`、`input`、`security`、`planning`、`tool`、`model`、`budget`、`finding`、`comment`、`checkpoint`、`report`、`correction` 和 `cleanup`。

最低标准事件集合：

```text
task.created
task.execution_started
task.stop_requested
task.stop_observed
task.paused
task.resume_started
task.resumed
task.terminated

input.acquire_started
input.acquired
input.rejected
input.normalized
input.scope_skipped

security.artifact_sanitized
security.artifact_blocked
security.boundary_failed
security.attestation_revalidated

planning.plan_created
planning.work_unit_created

tool.attempt_prepared
tool.attempt_started
tool.attempt_succeeded
tool.attempt_failed
tool.attempt_blocked
tool.attempt_interrupted

model.call_reserved
model.call_started
model.call_succeeded
model.call_failed_known
model.call_unknown
model.response_accepted
model.response_rejected

budget.authorization_initial
budget.reservation_created
budget.reservation_released
budget.usage_settled
budget.unknown_committed
budget.usage_overage_recorded
budget.freeze_applied
budget.authorization_added
budget.freeze_cleared
budget.account_closed

finding.candidate_created
finding.candidate_rejected
finding.validated
finding.merged
finding.confidence_assigned

comment.finalized
comment.trace_link_created
comment.superseded

checkpoint.created
checkpoint.reused
checkpoint.reconciled

report.generation_started
report.delivered
report.delivery_failed
report.delivery_unknown
report.delivery_reconciled

trace.event_corrected
trace.event_revoked
trace.link_corrected
```

事件类型不能由模型、仓库内容、工具结果或第三方异常动态产生。计划中的动作不能记录为已发生；只能记录“计划对象已创建”。

### 7.1 逐类型真实性约束

每个事件类型必须注册版本化 schema，schema 固定其必填权威对象、允许 payload、合法前驱和终态组合。最低约束如下：

| 事件 | 必填权威引用 | 必需前驱或条件 | 禁止情况 |
| --- | --- | --- | --- |
| `tool.attempt_started` | 已提交的 `tool_call_id`、attempt、工具声明版本 | `tool.attempt_prepared` | 工具未获计划授权 |
| `tool.attempt_succeeded` | 状态为 succeeded 的工具调用、安全结果引用 | `tool.attempt_started` | 失败、中断、阻断或结果未通过安全/schema 校验 |
| `tool.attempt_blocked` | 状态为 blocked 的工具调用、稳定阻断原因 | `tool.attempt_prepared` | 声称工具已开始或产生验证结果 |
| `tool.attempt_interrupted` | 状态为 interrupted 的工具调用、中断边界 | `tool.attempt_started` | 把本地确定性中断表达为外部结果 unknown |
| `task.stop_requested` | 已提交 StopRequest、action、stop revision、控制命令回执 | 任务未清理且 action 合法 | 修改 Task.version、夺取有效审查租约或伪造最终状态 |
| `task.stop_observed` | StopRequest、action/revision、当前 review_execution 或 stop_convergence 租约和 fencing | 执行者在安全边界观察请求 | 观察后又开始新外部 I/O |
| `model.call_reserved` | `model_call_id`、reservation、prepared request 摘要 | 调用准备事务 | 无预算预留或请求摘要不一致 |
| `model.call_started` | 状态为 running 的模型调用 | `model.call_reserved` | 外部 I/O 尚未获准 |
| `model.call_succeeded` | Provider 状态为 succeeded 的调用、usage 结算引用 | `model.call_started` | Provider 调用 unknown；响应业务处理结果由独立事件表达 |
| `model.call_failed_known` | failed_known 调用、稳定错误码、预算收敛引用 | `model.call_started` | 结果是否发生不确定 |
| `model.call_unknown` | unknown 调用、不确定消耗账本引用 | `model.call_started` | 请求可证明未发送 |
| `model.response_accepted` | Provider succeeded 调用、安全响应引用、response_state=accepted | `model.call_succeeded` | 响应未通过安全/schema 校验 |
| `model.response_rejected` | Provider succeeded 调用、稳定拒绝码、response_state=invalid_output/security_rejected | `model.call_succeeded` | 把拒绝响应作为候选或已验证事实 |
| `finding.candidate_created` | candidate ID、`model_claim` 或工具结果引用 | 对应调用及响应处理已收敛 | 把候选陈述记为已验证事实 |
| `finding.validated` | finding ID、位置/范围核对和确定性证据 | candidate 已创建 | 只有模型自述或工具未成功 |
| `finding.confidence_assigned` | finding ID、等级和依据事件 | 验证/降级决定已提交 | 缺少可核对依据 |
| `budget.usage_settled` | 真实账本条目、reservation 和 call | 调用完成事务 | Trace 自行计算或修改金额 |
| `checkpoint.created` | checkpoint ID、预算版本、事件范围 | 同事务业务状态完成 | 引用未提交事件或产物 |
| `comment.trace_link_created` | FinalFinding、直接证据和 checkpoint | finding 已 finalized | 直接证据为空或仅有任务级关联 |

适配器根据注册 schema 校验，不接受调用方自定义必填关系。后续新增事件类型必须同时增加 schema、真实性规则和合约测试，不能只增加字符串名称。

## 8. Payload 与安全引用

### 8.1 白名单结构

payload 只允许：

1. 标识、枚举、计数、布尔值、版本、时间和安全摘要；
2. 用途匹配的 `SanitizedArtifactRef`。

禁止保存 diff、源码、原始 prompt、原始模型回复、工具原始输出、第三方异常正文/堆栈、凭证、认证头、环境变量、未规范化 URL、secret 命中原文和模型内部思维。

### 8.2 Trace 用途

以下名称属于安全模块权威用途枚举中的 Trace 子集，Trace 模块只引用，不另行定义第二套枚举：

```text
trace_input_snapshot
trace_model_request
trace_model_response
trace_tool_input
trace_tool_result
trace_error_summary
trace_finding_evidence
trace_comment_summary
trace_report_summary
```

`model_egress`、`persistence` 或 `report_delivery` 的证明不能自动当作 Trace 用途。只有安全策略的版本化 `purpose_transition_matrix` 明确允许时，安全模块才能为同一不可变 artifact 创建目标 Trace 用途的新证明；Trace 写入只接受该目标证明，不自行判断兼容性。

写入和读取时校验证明存在、任务一致、来源/种类/用途匹配、摘要一致、决策为该用途允许的 `safe` 或 `redacted`、策略不弱于任务基线、仍在保留期且 provenance 未失效。失败时拒绝整个事务或返回受限视图，不回退展示原始内容。

### 8.3 模型与工具事实

模型 Trace 保存实际发送请求的安全引用和字节摘要、Provider/模型、输出模式、最大输出 token、经过结构化解析与白名单字段投影的响应引用、白名单 usage 和真实终态。不得把供应商普通 `content` 整段作为 Trace payload：只有契约定义的结构化字段可以持久化，疑似 reasoning、chain-of-thought、过程独白及无法分类的自由文本一律丢弃。模型产生的候选陈述必须标记为 `model_claim`，不能直接使用 `observation` 或 `validated_by`；只有权威业务状态或确定性证据核对后，才能由对应领域模块产生事实或验证事件。

工具事件必须区分计划、实际开始、成功、失败、blocked、interrupted、安全/schema 校验及是否被评论采用。本地确定性工具没有 unknown 终态；未运行不得创建 started/succeeded，失败、阻断或中断不得创建“验证通过”事实。

## 9. 因果与关联模型

### 9.1 边类型

因果边：

- `caused_by`：该事实由另一事实直接触发；
- `derived_from`：产物由上游材料派生；
- `validated_by`：发现由实际验证事实支持；
- `settled_by`：调用由预算结算事实收敛；
- `checkpointed_by`：事实被某 checkpoint 纳入一致边界；
- `supersedes`：新事实/链接替代旧表达。

非因果关联边：

- `relates_to`、`shares_call_with`、`included_in_report`。

边至少包含 `edge_id`、`task_id`、源/目标类型与 ID、关系、创建事件 ID和幂等键。禁止跨任务边；禁止把非因果关联展示为证据。

### 9.2 典型调用链

```text
安全请求证明
  -> budget.reservation_created
  -> model.call_reserved
  -> model.call_started
  -> model.call_succeeded / model.call_failed_known / model.call_unknown
  -> model.response_accepted / model.response_rejected
  -> budget.usage_settled / budget.unknown_committed
  -> checkpoint.created
  -> finding.candidate_created
  -> finding.validated / rejected
  -> comment.finalized
  -> comment.trace_link_created
```

预算超额在真实调用终态后追加 `budget.usage_overage_recorded -> budget.freeze_applied -> task.paused`，不能把预算冻结误写为模型调用失败。

## 10. 逐评论 `TraceLink`

`FinalFinding` 本身就是最终评论，不另设 Comment 聚合。每条 FinalFinding 恰好有一个当前有效 `TraceLink`：

| 字段 | 含义 |
| --- | --- |
| `trace_id` | 报告展示的稳定追踪标识 |
| `task_id / finding_id` | 权威对象关联；finding ID 即评论身份 |
| `link_version` | finding 内递增版本 |
| `direct_evidence_ids` | 该评论直接依赖的证据事件/对象 |
| `shared_process_ids` | 可与其他评论共享的调用/工具过程 |
| `validation_event_ids` | 位置、范围、工具或语义验证事实 |
| `confidence_event_id` | 置信度判定事实 |
| `budget_ledger_version` | 形成评论时预算版本 |
| `checkpoint_id` | 纳入评论的恢复边界 |
| `created_event_id` | 创建该链接的 Trace 事件 |
| `supersedes_trace_id` | 替代旧链接时填写 |

要求直接证据非空，且能到达真实读取的变更/上下文、实际工具结果或可核对语义依据。仅关联任务或模型调用不构成评论级追溯。多评论可共享调用，但不得自动共享彼此的直接证据。

发现更正或合并创建新版本和 `supersedes` 关系，不改写旧链接。报告默认展示当前有效版本，完整 Trace 可查看历史。

## 11. 追加写、更正和完整性

事件和边提交后不可更新 payload、类型、时间或关系。事实表达错误时追加：

- `trace.event_corrected`：补充正确结构并指向被更正事件；
- `trace.event_revoked`：声明旧事实不再可作为依据，但不删除；
- `trace.link_corrected`：创建新 `TraceLink` 版本。

更正不能改变权威业务状态；业务状态本身错误必须由其拥有模块通过合法领域操作修正，并在同一事务追加对应 Trace。

完整性校验至少检查任务内序号连续、边端点存在且同任务、payload 用途匹配、评论直接证据非空、预算事件引用真实账本条目、checkpoint 覆盖范围一致、无孤立 active 调用成功事件。发现损坏时返回 `trace_integrity_error`，不得把残缺集合展示为完整 Trace。

## 12. 公开契约

```text
prepare_events(task_id, business_transaction_kind, facts,
               expected_task_version, lease, idempotency_key)
  -> TraceMutationIntent

create_comment_link(final_finding, direct_evidence, shared_process,
                    validations, confidence, checkpoint,
                    expected_task_version)
  -> TraceMutationIntent

correct_event(target_event_id, correction, authority,
              expected_task_version)
  -> TraceMutationIntent

get_task_timeline(task_id, viewer, page_cursor)
  -> TraceTimelineView

get_comment_trace(trace_id, viewer)
  -> CommentTraceView

get_call_trace(model_call_id | tool_call_id, viewer)
  -> CallTraceView

verify_integrity(task_id)
  -> TraceIntegrityResult
```

`TraceMutationIntent` 至少携带稳定 Intent ID、任务 ID、事件/边/链接、业务事务类型、幂等键和安全摘要；普通任务事件还携带预期 Task.version。执行面事务必须携带有效租约与 fencing token。StopRequest 创建是独立控制流，只携带 `stop_revision`、观察到的任务版本和类型化可信用户操作，不条件更新 Task 行；它允许在有效审查租约存在时写入 `task.stop_requested`，但不能携带或取代执行者 fencing token，也不能写 stop_observed/paused/terminated。相同键相同摘要重放返回原结果；同键不同摘要返回冲突。

## 13. 原子事务

Trace 模块不自行提交跨模块事务。

- **任务创建**：任务、预算账户、创建 Trace 和幂等身份共同提交；
- **调用准备**：安全证明、预算预留、调用 `reserved`、请求 Trace 和工作单元关系共同提交；
- **调用开始**：调用转 `running` 与 `model.call_started` 共同提交，成功后才发外部请求；
- **调用完成**：安全响应/错误、预算结算、调用终态、候选结果、Trace 和 checkpoint 共同提交；
- **预算超额第一阶段**：在调用完成事务原子提交实际 usage、超额、预算冻结、调用/执行结果、对应 Trace、checkpoint 和“需要确定性收敛”的意图；保留审查租约，但禁止新的外部调用；
- **预算超额第二阶段**：发现处理完成后原子提交 FindingSet、范围、逐发现 TraceLink、`partial` 快照、`task.paused` 和租约释放。候选发现不能直接进入快照；任一阶段失败均停止新的外部调用；
- **恢复核对**：未完成调用转 unknown、不确定消耗、Trace、checkpoint 和结果快照共同提交；
- **评论形成**：FinalFinding、置信度事实和 `TraceLink` 共同提交；FinalFinding 本身即最终评论；
- **报告交付**：交付状态、产物摘要和报告 Trace 共同提交；
- **清理**：删除完整 Trace 及全部任务详细数据并写墓碑，全有或全无。
- **停止请求**：控制面原子提交包含 action/stop_revision 的 StopRequest、控制命令回执和 task.stop_requested，不更新 Task.version；执行者随后以 review_execution 或 stop_convergence 租约提交 task.stop_observed，最终按 action 与快照、task.paused 或 task.terminated、请求完成状态和租约释放共同提交。

清理完成不写入将被同一事务删除的 Trace 事件；结果仅保存在任务墓碑和独立的白名单安全运维记录中。

业务事务回滚时，对应 Trace 不得单独存在；禁止“稍后补写 Trace”把不一致状态包装为成功。

## 14. 查询视图

### 14.1 任务时间线

按 sequence 分页展示安全摘要、真实状态、关联对象和因果关系。预算金额从指定账本版本读取；Trace 中的展示快照仅用于历史说明，不参与预算决策。

### 14.2 评论追溯

展示评论位置和稳定标识、直接证据、安全处理限制、实际模型/工具过程、验证与置信度事实、预算版本、预算 usage 可信度（`missing`/`untrusted` 预留及 `pending_revalidation` 阻塞标记）、checkpoint 和缺失/受限项。不展示模型内部思维。

### 14.3 查询状态

统一返回：

- `available_complete`：完整且校验通过；
- `available_restricted`：因安全策略只能展示部分结构；
- `expired`：已到清理资格时间但尚未删除；
- `cleanup_failed`：清理失败，数据仍完整保留；
- `cleaned`：仅剩墓碑；
- `integrity_error`：存在损坏，禁止冒充完整；
- `not_found`：从未存在或墓碑已显式清除。

查询只读，不延长恢复期或保留期，不修改任务状态。

## 15. 并发与幂等

- `(task_id, sequence)`、`event_id`、任务内 `idempotency_key` 唯一；
- 同一 FinalFinding 的同一 `link_version` 唯一，当前有效链接最多一个；
- 事务内事件按确定规则分配连续序号；
- 执行面写入携带任务版本、租约 ID 和 fencing token；普通控制面写入携带预期任务版本并证明无有效租约；StopRequest 创建只携带独立 stop_revision 和可信操作，可与有效审查租约并存，不更新 Task.version 或推进执行状态；
- 旧执行者不能追加迟到事件、边或链接；
- 同一 Intent 重放不重新分配序号；
- Trace 查询可并发，但不能看到未提交的部分事务集合。

## 16. 错误模型

| 错误码 | 行为 |
| --- | --- |
| `trace_event_invalid` | 拒绝不符合 schema 的事件 |
| `trace_payload_unsafe` | 拒绝未安全包装 payload |
| `trace_artifact_purpose_mismatch` | 拒绝用途不匹配引用 |
| `trace_cross_task_reference` | 拒绝跨任务事件、边或链接 |
| `trace_causation_missing` | 拒绝必需因果边缺失 |
| `trace_comment_evidence_missing` | 不允许形成最终评论链接 |
| `trace_idempotency_conflict` | 同键不同内容，事务不生效 |
| `trace_version_conflict` | 重新读取后再决定，不自动补写 |
| `trace_integrity_error` | 停止依赖该 Trace 的继续执行/展示 |
| `trace_expired / trace_cleaned` | 返回对应只读状态 |

错误只含白名单字段和安全引用，不包含原始材料或第三方异常。

## 17. 保留与清理

- 未结束任务的 Trace 恢复有效期与最近有效 checkpoint 一致；
- 未结束任务在恢复有效期（七天）过后仍保留其 Trace 与 checkpoint，任务保留为不可恢复状态；在任务进入终态前按最近有效 checkpoint 持续可查询；其详细数据不自动删除，但达到清理资格（与任务模块 `CleanupTask` 一致：未结束任务恢复期已过）后可由用户显式清理，清理时墓碑说明任务因过期清理而非自然终态；
- 任务进入终态时，完整 Trace、checkpoint、预算明细、安全证明和调用明细统一使用 `retention_expires_at = terminated_at + 168h`；
- 到期仅代表具备清理资格，不在读取时自动删除；
- 显式幂等清理在单一事务删除全部详细数据并留下最小墓碑；
- 清理失败时回滚全部删除，并通过独立安全运维记录标记失败；
- 墓碑不保存事件、边、评论证据、源码或 Trace payload，只表明任务曾存在、最终状态、到期和清理结果；报告产物标识及安全规范化路径由任务模块的墓碑保存（见任务模块 13.3），Trace 墓碑不再重复保存。

## 18. 持久化逻辑结构

| 集合 | 写入模式 | 关键约束 |
| --- | --- | --- |
| `trace_events` | 只追加 | `(task_id, sequence)`、`event_id` 唯一 |
| `trace_edges` | 只追加 | 端点存在、同任务、关系枚举固定 |
| `comment_trace_links` | 只追加版本 | `(finding_id, link_version)` 唯一；finding ID 即最终评论身份 |
| `trace_intents` | 创建后只读 | 幂等键与内容摘要唯一匹配 |
| `trace_integrity_checks` | 只追加 | 仅白名单结果，不复制 payload |

索引至少支持任务时间线、评论 trace、模型/工具调用、checkpoint 和清理保留组查询。物理实现不得提供绕过安全引用的通用 payload 读取接口。

## 19. 关键不变量

1. Trace 只记录已发生事实，不决定业务流程；
2. 事件只追加，更正不改写历史；
3. 事件、边和链接不得跨任务；
4. Trace payload 只接受用途匹配的安全引用或白名单结构；
5. 未运行工具不得出现成功/验证事件；
6. 未获得响应不得出现模型成功事实；
7. 不记录模型内部思维；
8. 每条最终评论有独立且直接证据非空的 `TraceLink`；
9. 共享调用不等于共享直接证据；
10. 预算 Trace 引用权威账本，不维护第二事实源；
11. 业务状态与对应 Trace 同事务全有或全无；
12. old fencing token 不能追加事件；
13. 清理要么删除完整详细集合，要么全部保留；
14. 查询不得把受限或损坏集合显示为完整；
15. 不同任务的 Trace 不串写。

## 20. 测试设计

### 20.1 领域与性质测试

- 所有标准事件 schema、版本和分类；
- 任务内 sequence 连续、事件只追加和更正链；
- 因果边端点、同任务约束和禁止时间推断；
- 每条评论直接证据非空，共享调用下证据隔离；
- 未执行工具、失败工具、unknown 模型不会产生虚假成功；
- reasoning 字段、原始异常和 secret 无法进入 payload；
- Hypothesis 生成事件图，证明无跨任务边、无孤立链接和无非法循环替代链。

### 20.2 安全测试

- 对 diff、prompt、响应、工具结果、URL、错误和摘要注入模拟 secret；
- 用途不匹配、证明缺失、摘要不匹配和策略失效均拒绝；
- 查询受限时不回退到原始内容；
- Trace 与 SecurityLog 投影均不保存命中原文。

### 20.3 事务与故障注入

- 任务创建、调用准备/开始/完成、预算超额、unknown 核对、评论形成和报告交付各边界中断；
- 证明业务状态和 Trace 全有或全无；
- 相同 Intent 重放不重复事件或序号；
- 旧 fencing token 和旧任务版本被拒绝；
- 清理中断后完整 Trace 仍可查询且状态为 `cleanup_failed`。

### 20.4 查询与验收

- 任务时间线顺序稳定并支持分页；
- 评论 trace 能到达真实输入、调用、验证、预算和 checkpoint；
- 同一调用支持多评论但各评论证据不同；
- available/restricted/expired/cleaned/integrity_error/not_found 明确区分；
- 终态后 168 小时边界和显式清理；
- 报告中的 trace ID 可定位到当前有效 `TraceLink`。

## 21. ADR

### ADR-TRACE-001：权威状态与追加事实事件并存

- **决定**：任务、预算和调用保留各自权威状态，Trace 在同一事务追加事实。
- **原因**：满足可追溯性而不引入完整事件溯源复杂度。
- **代价**：需要跨模块一致性校验和明确事实所有者。

### ADR-TRACE-002：评论级 Trace ID 指向 `TraceLink`

- **决定**：报告中的 trace ID 不是任务或调用 ID，而是评论依据集合的稳定标识。
- **原因**：一条评论可能依赖多个共享和直接过程，同一调用也可能产生多条不同评论。
- **代价**：需要维护链接版本及证据边。

### ADR-TRACE-003：禁止持久化内部思维和未安全原文

- **决定**：仅保存真实输入/输出的安全引用和结构事实，不保存 reasoning 或原始 payload。
- **原因**：满足安全边界，并避免把不可验证内部思维当作证据。
- **代价**：某些供应商调试细节不可用，但不影响业务追溯。

## 22. 后续模块契约

- 任务模块提供稳定任务版本、租约、fencing、checkpoint 和统一保留组；
- 预算模块提供账本条目 ID、版本和不可变摘要，Trace 不自行计算余额；
- 执行模块为每次模型/工具尝试提供稳定标识和真实状态；
- 发现处理模块在形成评论时提交直接证据、验证和置信度事实；
- 报告模块展示评论级 `trace_id`，不泄露受限 payload；
- 安全模块提供 Trace 用途证明和 provenance 核对；
- SQLite 适配器保证事件序号、外键、幂等和跨模块原子工作单元。
