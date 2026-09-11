# 任务 16 本地 Diff 编排实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 使用现有领域服务完成单进程 plain diff 到真实 Markdown 报告的闭环。

**架构：** 应用层保存不可变运行记录，使用 `ExecutionSession` 驱动单调阶段；
输入、规划、执行、发现处理和报告规则继续由现有领域服务负责。首版使用内存
任务服务和协议化依赖，查询服务只读。

**技术栈：** Python 3.12、dataclasses、pytest、pytest-asyncio、现有领域对象和
`MarkdownOutputAdapter`。

---

## 文件结构

- 创建 `src/code_review_agent/application/dto.py`：启动命令、运行结果、进度和
  Trace 的不可变 DTO。
- 创建 `src/code_review_agent/application/orchestration.py`：阶段枚举、会话、
  依赖协议、顺序调度和结果收敛。
- 创建 `src/code_review_agent/application/task_service.py`：创建任务、配置依赖、
  启动审查并保存运行记录。
- 创建 `src/code_review_agent/application/query_service.py`：读取任务进度与 Trace。
- 创建 `src/code_review_agent/application/__init__.py`：公开应用层入口。
- 创建 `tests/integration/test_plain_diff_review_flow.py`：真实领域服务加 fake
  provider/tool 的端到端测试。

### 任务 1：先锁定主流程行为

**文件：** `tests/integration/test_plain_diff_review_flow.py`

- [ ] 编写测试，构造 `TaskSpec`、`InputService`、`ReviewPlanner`、
  `WorkUnitExecutor`、`FindingProcessor`、`ReportBuilder` 和
  `MarkdownOutputAdapter`，启动服务后断言：
  `ReviewRunResult.result_state` 为 `complete_no_findings`，报告文件存在且包含
  审查结论，fake model 的发送次数等于计划工作单元数。
- [ ] 编写测试断言工作单元按 `execution_rank` 顺序发送，并且
  `get_progress()`、`get_trace()` 不改变任务版本或租约。
- [ ] 运行：
  `pytest tests/integration/test_plain_diff_review_flow.py -q`
  预期：因 `code_review_agent.application` 不存在而失败。

### 任务 2：实现最小 DTO 和单调阶段机

**文件：** `src/code_review_agent/application/dto.py`,
`src/code_review_agent/application/orchestration.py`

- [ ] 定义 frozen DTO：`StartReviewCommand`、`ReviewRunResult`、
  `ReviewProgressView`、`TraceEventView`；字段只使用任务 ID、阶段、结果状态、
  交付路径/摘要和不可变 tuple。
- [ ] 定义 `SessionPhase`，实现 `ExecutionSession.advance(target)`；只允许固定
  后继，重复或逆向阶段抛出 `ValueError("illegal_phase_transition")`。
- [ ] 定义 `ReviewDependencies` 协议化容器和
  `ReviewOrchestrator.run(command, dependencies)`，调用链固定为：
  `normalize_plain_diff`、`plan`、按 `execution_rank` 执行、
  `FindingProcessor.process`、构造报告 `ResultSnapshot`、`ReportBuilder.build`
  和 `deliver`。
- [ ] 使用 `uuid4()` 和现有 digest 工具生成 checkpoint/快照标识；不在应用层
  解析 diff、验证候选或计算预算。

### 任务 3：实现结果收敛和应用服务

**文件：** `src/code_review_agent/application/task_service.py`,
`src/code_review_agent/application/query_service.py`

- [ ] 创建任务并通过领域 `TaskService.acquire_execution()` 获取租约；输入成功后
  用 `bind_input()`，每个阶段用连续 `Checkpoint` 调用 `complete_phase()`。
- [ ] 根据执行结果和 `FindingSet.coverage` 选择：
  `no_changes`、`complete_no_findings`、`complete_with_findings`、`partial`
  或 `unknown`；模型 unknown 后停止执行循环。
- [ ] 组装完整 `domain.report.models.ResultSnapshot`，报告只从该快照构建；报告
  交付失败返回失败信息但不改写审查结果。
- [ ] `ReviewQueryService` 只从内存记录返回进度和 Trace 的副本，不调用
  `renew_lease()`，不写任务。

### 任务 4：补齐异常和停止边界测试

**文件：** `tests/integration/test_plain_diff_review_flow.py`

- [ ] 为预算不足、安全跳过和工具失败添加测试，断言报告明确标为 `partial`
  并包含覆盖限制。
- [ ] 为模型输出无效和模型 unknown 添加测试，断言分别是已知失败/unknown，
  后续 fake model 不再发送。
- [ ] 添加停止检查 fake，在第二个工作单元前返回停止请求，断言第二个工作单元
  没有外部调用且已有结果仍进入确定性收敛。

### 任务 5：验证、整理和提交

- [ ] 运行：
  `pytest tests/integration/test_plain_diff_review_flow.py -q`
- [ ] 运行：
  `pytest -q`
- [ ] 运行：
  `ruff check src tests`
- [ ] 运行：
  `mypy`
- [ ] 运行 `git diff --check`，确认只包含任务 16 实现、测试和应用文档。
- [ ] 提交：
  `git add src/code_review_agent/application tests/integration/test_plain_diff_review_flow.py`
  `git commit -m "feat: complete local diff review flow"`
