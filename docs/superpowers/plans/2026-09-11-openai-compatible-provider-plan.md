# OpenAI-Compatible Provider 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 从 TOML 配置装配真实 OpenAI-compatible HTTP Provider，并完成一次
可生成 Markdown 报告的审查。

**架构：** Provider 实现现有 `ModelGatewayPort`，prepare 阶段只序列化固定请求，
send 阶段只发送一次并归一化响应。Bootstrap 根据配置选择离线或 HTTP Provider，
应用编排、预算、安全和发现处理保持不变。

**技术栈：** Python 3.12、httpx、respx、pytest、TOML。

---

### 任务 1：配置文件安全校验

**文件：**
- 修改：`src/code_review_agent/config.py`
- 修改：`code-review-agent.toml`
- 创建：`code-review-agent.toml.example`
- 创建：`tests/unit/test_config.py`

- [ ] 写失败测试：解析 OpenAI-compatible 字段，拒绝缺 token、HTTP origin、
  非绝对 path、非正整数 timeout/大小限制和 group/other 可读的含 token 文件。
- [ ] 运行 `uv run pytest tests/unit/test_config.py -q`，确认因字段不存在失败。
- [ ] 扩展 frozen 配置 DTO 和严格 TOML 解析；只读取文件，不读取环境变量。
- [ ] 再运行测试，确认通过。

### 任务 2：实现 HTTP Provider

**文件：**
- 创建：`src/code_review_agent/adapters/model/openai_compatible.py`
- 创建：`tests/contract/test_openai_compatible_provider.py`

- [ ] 写失败测试：固定请求 body/header/digest、ownership、discard、成功响应和
  单次发送。
- [ ] 使用 `respx` 写 401、429、500、timeout、非法 JSON、非法 choices 和
  超大响应测试，断言稳定 `failed_known/unknown`。
- [ ] 实现 `OpenAICompatibleProvider`，使用 `httpx.AsyncClient`，关闭 redirect、
  streaming 和自动重试。
- [ ] 验证 token 不进入异常、响应对象或测试日志。

### 任务 3：接入 Bootstrap

**文件：**
- 修改：`src/code_review_agent/bootstrap.py`
- 修改：`tests/integration/cli/test_cli_commands.py`
- 创建：`tests/integration/model/test_openai_provider_review.py`

- [ ] 写失败测试：`provider.id=openai-compatible` 时选择 HTTP Provider，并用
  mock HTTP 响应完成一次 CLI review。
- [ ] 将 Provider 构造从 `_ConfiguredSteps` 外部注入；`local` 和
  `openai-compatible` 都通过同一端口。
- [ ] 运行集成测试，确认生成真实 Markdown 且预算 usage 与响应一致。

### 任务 4：完整验证

- [ ] 运行 `uv run pytest -q`。
- [ ] 运行 `uv run ruff check src tests`。
- [ ] 运行 Provider、bootstrap 和 config 的 Mypy。
- [ ] 搜索 `os.environ`、`getenv`、`from_environment`，确认无环境变量配置。
- [ ] 运行一次 mock HTTP 的端到端审查并读取报告。
- [ ] `git diff --check` 后提交实现。
