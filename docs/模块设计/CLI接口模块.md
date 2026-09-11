# Code Review Agent CLI 接口模块详细设计

## 1. 文档状态

- 日期：2026-09-10
- 状态：设计完成，待复审
- 命令入口：`code-review-agent`

## 2. 设计结论

CLI 是 Typer/Rich 薄适配器，只校验参数语法、类型和互斥，转换稳定应用 DTO，并展示已持久化状态。它不访问 repository、SQLite、SDK、keyring 或领域模块，不实现预算、恢复、清理、Trace 完整性和任务迁移。

stdout 仅输出最终结果，进度/警告/错误写 stderr。`--json` 时 stdout 是单个稳定 JSON 信封，禁用样式和动画。

## 3. 命令与 DTO

```text
review / resume TASK_ID / status TASK_ID / trace show TRACE_ID
credentials set|clear|status / providers list
terminate TASK_ID / cleanup TASK_ID
```

通用选项为 `--json`、`--no-color`、`--verbose`、`--request-id UUID` 和 `--help`。verbose 仅增加已脱敏摘要。`--request-id` 是可选调用方覆盖标识：提供时作为本次命令对应应用用例的 `request_id`（实现幂等重放），未提供时由 CLI 为每个控制操作分别生成 `request_id`；`CliResponseEnvelope` 始终回显本次实际使用的 request ID，二者关系以"显式覆盖优先、缺省自动生成"为准。当一条命令展开为多个应用用例（如 resume 可能包含 AddBudgetAuthorization + ResumeReview + RetryReportDelivery）时，仅首个用例使用调用方提供的 `--request-id`，其余用例由 CLI 派生独立的新 `request_id` 并在信封中回显各用例的 request ID 映射，避免复用同一覆盖 ID 造成预算模块"授权 request_id 唯一"冲突。

`CliResponseEnvelope` 包含 schema version、command、request ID、ok、data、error、warnings。错误只含稳定 code/category/message/stage/recoverable/task/trace ID、next actions 和标量 details。

`TaskStatusView` 分别输出 control state、phase、result state、delivery state、availability、recoverability、reason、固定输入、Provider/模型、预算、报告、checkpoint、期限和 next actions。不得把“审查完成、报告失败”折叠成审查失败。

## 4. review 与 resume

```text
review (--diff-file PATH | --url URL | --stdin)
       --provider PROVIDER --model MODEL
       [--budget-tokens N]
resume TASK_ID [--add-budget-tokens N] [--confirm-unknown-retry]
```

review 三种输入恰好一个；未指定且 stdin 非 TTY 时等价于 `--stdin`，TTY 不等待隐式输入。CLI 只构造来源 DTO；UTF-8、diff、规模、完整性和路径安全由输入用例处理。URL 语法层只接受 HTTPS GitHub.com PR/GitLab.com MR，拒绝 userinfo/query/fragment。预算默认 50000，语法范围 1..1000000。MVP 不接受自定义输出路径，报告位置始终由应用层按 task ID 派生为 `reports/<task_id>.md`，成功响应返回该规范化位置。

追加预算和 resume 是两个显式应用操作：先幂等追加，再恢复。每个控制操作分别生成 `request_id` 和请求指纹，并携带调用前取得的 `expected_task_version` 与类型化本机可信用户操作凭据；版本冲突后重新读取状态，不复用旧版本盲重试。unknown 未确认时不调用外部系统；确认也不能绕过新预留。固定条件变化返回 `new_task_required`。severity 为内部字段，MVP CLI/Markdown 不展示标签。

## 5. 查询、凭据、终止和清理

`status` 只调用 GetTaskStatus，区分有效运行、过期租约、暂停、unknown、终止、过期、清理失败、已清理和不存在，不续期。

`trace show TRACE_ID [--cursor CURSOR] [--limit 1..500]` 查询评论级 TraceLink，区分 complete/restricted/expired/cleanup_failed/cleaned/integrity_error/not_found；不把受限或损坏数据冒充完整，不显示模型内部思维。

credentials set 默认隐藏 TTY 输入，非交互必须 `--token-stdin`；clear 非交互要求 `--yes`；status 只显示 configured 状态，不显示掩码、长度或前后缀。providers list 只读取 Registry catalog 和凭证存在性，不网络探测。

terminate 使用稳定 reason code，是协作式控制，不跨进程强杀、不删除数据、不清零预算、不把 partial 标完整。cleanup 在 TTY 确认，非交互要求 `--yes`；资格与原子删除由应用层判断，保留 Markdown 和墓碑，失败时详细数据完整保留。resume、预算追加、terminate、报告重试和 cleanup 统一映射 `ControlCommand` 元数据：`task_id`、`expected_task_version`、类型化可信用户操作凭据、`request_id` 和安全请求指纹；具体命令字段另行附加。其中"报告重试"没有独立 CLI 命令，由 `resume TASK_ID` 按任务状态路由：任务审查已完成或已终止、报告交付为 `pending/failed/unknown` 时，resume 隐式触发 `RetryReportDelivery` 并标注为报告重试；任务处于 `paused`（含 `budget_anomaly` 冻结）且已有可交付快照、交付状态为 `pending/failed/unknown` 时，resume 在完成恢复校验后也允许路由到报告重试（与任务模块"冻结期间允许报告交付"一致）；不单独暴露 `retry-report` 命令。

## 6. 输出、信号与退出码

非 TTY 禁用颜色/动画但不自动改 JSON。进度只展示应用发布的已持久化阶段、单元计数和预算。第一次 SIGINT/SIGTERM 调用 RequestPause 并等待安全边界；第二次 SIGINT 立即退出，不声称已暂停；外部结果不明由恢复转 unknown。

| 码 | 类别 |
| --- | --- |
| 0 | 命令成功，包括诚实产生 partial/unknown 报告 |
| 2 | CLI 用法错误 |
| 3 | 对象不存在 |
| 4 | 版本、租约、fencing 或幂等冲突 |
| 5 | 需要凭证、确认、预算或新任务 |
| 6 | 外部明确失败 |
| 7 | 安全阻断 |
| 8 | 持久化/完整性失败 |
| 9 | 报告交付失败/unknown |
| 10 | 已过期或已清理 |
| 70 | 未分类内部错误 |
| 130/143 | SIGINT/SIGTERM 未完成优雅退出 |

退出码不替代业务状态，human 与 JSON 模式一致。

## 7. 用例映射、测试与 ADR

review→StartReview；resume→可选 AddBudgetAuthorization + ResumeReview（任务处于暂停或已终止、报告待重试时隐式触发 RetryReportDelivery）；status→GetTaskStatus；trace→GetCommentTrace；credentials→CredentialApplicationService；providers→ListProviderCatalog；terminate→TerminateReview；cleanup→CleanupTask。CLI 不补查 repository 或解析错误文本。

测试参数互斥、stdin/TTY、URL/预算边界、DTO 映射、stdout/stderr、JSON golden、Rich 快照、退出码、凭证不回显、确认和信号；架构测试禁止 CLI 导入 repository、SDK、keyring 和领域实现。

ADR-CLI-001：human/JSON 共享 DTO。ADR-CLI-002：退出码按稳定类别分配。ADR-CLI-003：信号映射为协作式暂停。
