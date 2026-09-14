# P0 安全与 Trace 修复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 修复 checkpoint 任意模块导入、逐评论 Trace 不可查询和本地凭证被跟踪三个 P0 问题。

**架构：** checkpoint 仅从显式可信类型注册表恢复对象；评论 Trace 通过 SQLite 映射到任务与 work unit；本地配置仅由被忽略的运行文件承载。

**技术栈：** Python 3.12、SQLite、pytest、Typer。

---

### 任务 1：锁定 checkpoint 类型边界

**文件：**
- 修改：`src/code_review_agent/application/persistence.py`
- 修改：`tests/integration/recovery/test_persistent_recovery.py`

- [x] 添加测试，断言仓库类型在 import 前被拒绝。
- [x] 运行测试并确认因现有动态 import 失败。
- [x] 添加生产类型精确白名单并移除动态 import。
- [x] 将恢复集成测试改用生产执行结果类型。
- [x] 运行恢复测试确认通过。

### 任务 2：持久化并解析评论 Trace

**文件：**
- 修改：`src/code_review_agent/application/persistence.py`
- 修改：`src/code_review_agent/bootstrap.py`
- 修改：`tests/integration/model/test_openai_provider_review.py`
- 修改：`tests/integration/test_trace_persistence.py`

- [x] 添加端到端测试，使用报告中的 Trace ID 查询对应事件。
- [x] 运行测试并确认返回 `task_not_found`。
- [x] 增加评论 Trace 映射、保留期和清理逻辑。
- [x] 在 finding 校验时写入对应 work unit 关联。
- [x] 运行 Trace 和模型集成测试确认通过。

### 任务 3：移除被跟踪的本地凭证配置

**文件：**
- 修改：`.gitignore`
- 删除：`code-review-agent.toml`
- 修改：`README.md`
- 修改：`src/code_review_agent/cli/app.py`

- [x] 删除被跟踪的本地运行配置。
- [x] 明确从示例复制且本地文件不提交。
- [x] 将配置读取延迟到 CLI 启动，保证无本地配置时模块仍可导入。
- [x] 检查 Git 状态不再包含凭证明文。

### 任务 4：完整验证

**文件：**
- 检查：`src/`、`tests/`、文档与 Git diff。

- [x] 运行 P0 定向测试。
- [x] 运行完整 pytest、Ruff、mypy。
- [x] 运行 `git diff --check` 并审查最终 diff。
