# 任务 16：本地 Diff 审查编排设计

## 目标

在不引入 CLI 和完整 SQLite 事务面的前提下，打通单进程本地 plain
diff 到 Markdown 报告的最小闭环，并提供稳定的应用层 DTO、单调阶段机和
无副作用查询服务。

## 范围

本任务创建：

- `application/dto.py`：应用命令、运行结果、进度和 Trace 视图。
- `application/orchestration.py`：`ExecutionSession`、单调阶段机和流程编排。
- `application/task_service.py`：创建任务并启动一次本地 diff 审查。
- `application/query_service.py`：只读状态和 Trace 查询。
- `tests/integration/test_plain_diff_review_flow.py`：覆盖主链路与诚实收敛。

CLI、暂停恢复命令、跨进程租约恢复和完整 SQLite 执行事务面分别留给任务
17、18 及后续持久化任务。

## 方案选择

### 采用：薄应用层加协议化依赖

应用编排持有流程顺序和停止边界，通过现有输入服务、规划器、工作单元执行器、
发现处理器、报告构建器和输出端口完成工作。任务状态由现有领域
`TaskService` 写入，应用层不复制领域规则。

优点是能复用已完成能力、测试替身简单，并能在后续把进程内任务存储替换为
SQLite。限制是首版不承诺进程重启恢复。

### 未采用：直接在编排器中实现领域规则

实现速度表面上更快，但会重复预算、安全、覆盖和报告判定，破坏模块边界。

### 未采用：先补齐完整 SQLite 事务面

这会把任务 16 扩展成持久化重构，并阻塞已具备的本地最小闭环，超出当前
里程碑范围。

## 应用接口

`StartLocalDiffReview` 接收 diff 文本或文件、输出路径、任务规格和调用方
标识。`LocalDiffReviewService.start()` 创建任务并委托编排器执行，返回
`ReviewRunResult`，其中包含任务 ID、会话阶段、权威任务状态、结果状态、
报告交付信息和可读限制。

`ReviewQueryService.get_progress()` 和 `get_trace()` 只读取应用存储的不可变
记录。查询不获取或续约租约，不改变任务版本、阶段、控制状态或交付状态。

## 阶段与数据流

`ExecutionSession` 按以下顺序单调推进：

```text
session_acquired
→ input_normalizing
→ planning
→ executing
→ consolidating
→ snapshotting
→ reporting
→ completed
```

每次推进必须是当前阶段的直接后继；重复阶段和逆向推进均拒绝。流程数据依次
形成固定输入、冻结计划、按 `execution_rank` 排序的执行结果、确定性
FindingSet、报告快照和交付结果。

首版应用快照使用报告领域的完整 `ResultSnapshot`，任务聚合继续保存其轻量
快照引用。二者通过 task ID、checkpoint ID、结果状态和内容摘要绑定。

## 停止和异常收敛

编排器在每个工作单元开始前调用停止检查器。观察到停止后，不再调用工具或
模型，已有执行事实进入发现收敛。

执行结果按事实决定最终状态：

- 所有可审查范围完成：`complete_no_findings` 或
  `complete_with_findings`。
- 预算不足、安全跳过、工具失败或模型明确失败导致覆盖缺失：`partial`。
- 任一模型调用结果不确定：`unknown`，并停止后续外部调用。
- 输入无法固定或核心持久化/安全边界失败：抛出应用错误，不声称已完成审查。

无论降级原因是什么，报告必须列出未审查、不可审查或 unknown 范围。

## Trace

任务 16 只提供应用编排所需的评论 Trace 视图：阶段推进、工作单元结果、
收敛结果和报告交付。Trace 记录使用不可变、按序事件；查询返回副本，不能
暴露可修改的内部集合。

## 测试

集成测试先使用 fake model、fake tools 和真实领域服务验证：

- plain diff 能生成第一份真实 Markdown 报告。
- 工作单元严格按 `execution_rank` 串行执行。
- 阶段只能单调推进。
- 预算不足、安全跳过、工具失败和模型输出无效会诚实收敛。
- unknown 或停止请求出现后不再产生新的外部调用。
- 状态和 Trace 查询前后任务版本及租约不变。

测试之外再运行 Ruff、Mypy 和全量 Pytest，确认应用接口没有破坏现有模块。
