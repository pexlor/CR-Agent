# 配置化金额预算实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 根据配置中的单次 CNY 上限和模型每百万 token 单价生成任务 token 授权，并在报告中展示金额使用情况。

**架构：** 配置层使用 Decimal 完成金额到 token 的确定性换算；CLI 不接受 token 预算参数；领域预算账本保持 token 计量，报告层投影 CNY。

**技术栈：** Python 3.12、Decimal、TOML、Typer、pytest。

---

### 任务 1：配置解析与换算

**文件：**
- 修改：`src/code_review_agent/config.py`
- 修改：`config.example.toml`
- 修改：`tests/unit/test_config.py`

- [ ] 添加有效金额换算和无效金额/单价/上限测试。
- [ ] 运行测试确认现有实现失败。
- [ ] 用 Decimal 实现金额配置解析及向下取整换算。
- [ ] 运行配置测试确认通过。

### 任务 2：移除 CLI token 参数注入

**文件：**
- 修改：`src/code_review_agent/cli/commands.py`
- 修改：`src/code_review_agent/bootstrap.py`
- 修改：`tests/integration/cli/test_cli_commands.py`
- 修改：相关 runtime 合约测试。

- [ ] 添加 CLI 使用配置换算预算的失败测试。
- [ ] 删除 `--budget-tokens` 并收窄 runtime review 接口。
- [ ] 运行 CLI 与模型集成测试确认通过。

### 任务 3：金额报告与恢复绑定

**文件：**
- 修改：`src/code_review_agent/application/dto.py`
- 修改：`src/code_review_agent/bootstrap.py`
- 修改：`src/code_review_agent/resources/templates/review.md.j2`
- 修改：`tests/integration/test_markdown_delivery.py`

- [ ] 添加金额报告失败测试。
- [ ] 投影并渲染 CNY 金额字段。
- [ ] 将价格和金额配置加入 checkpoint 绑定摘要。
- [ ] 运行报告、预算和恢复测试。

### 任务 4：完整验证

**文件：**
- 修改：`README.md`
- 修改：`docs/已知限制.md`
- 修改：`docs/测试与评估报告.md`

- [ ] 更新配置及使用说明。
- [ ] 运行完整 pytest、Ruff、mypy 和 `git diff --check`。
- [ ] 独立代码审查无 Critical/Important。
