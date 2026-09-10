# Code Review Agent 技术选型设计

## 1. 文档状态

- 日期：2026-09-10
- 状态：已确认技术路线，待进入实现计划
- 产品形态：单机、同一操作系统账号下使用的纯 CLI 工具
- MVP 输入：UTF-8 unified diff 文本／文件、GitHub.com PR、GitLab.com MR
- MVP 输出：独立 Markdown 报告，不向远程平台回评
- 支持语言：Python、JavaScript、TypeScript

本文基于根目录的《需求拆解.md》《相似项目实现参考.md》和《开发流程.md》，记录技术栈、组件边界及关键取舍。需求文档中的确认记录优先于早期建议和仍残留的旧待确认描述。

## 2. 选型摘要

| 层级 | 选择 | 用途 |
| --- | --- | --- |
| 语言与运行时 | Python 3.12 | CLI、编排、平台和模型接入 |
| 项目与依赖管理 | uv、`pyproject.toml`、`uv.lock` | 跨平台可复现安装、构建和命令入口 |
| CLI | Typer、Rich | 子命令、参数校验、进度和错误展示 |
| 数据模型与配置 | Pydantic v2、pydantic-settings、TOML | 领域对象、模型输出和配置校验 |
| 模型适配 | LangChain ChatModel adapters | OpenAI、Anthropic、OpenAI-compatible 接入 |
| 平台适配 | PyGithub、python-gitlab、受限 HTTPX | GitHub/GitLab 元数据、diff 和固定版本文件读取 |
| Diff 解析 | unidiff + 项目严格校验层 | hunk、文件状态和新旧行号解析 |
| 持久化 | Python `sqlite3` | checkpoint、预算流水、trace、评论和任务状态 |
| 凭证 | keyring | 操作系统账号级安全凭证存储 |
| Secret 检测 | detect-secrets + 项目自有规则 | 出站和落盘前检测、脱敏或跳过 |
| 报告 | Jinja2 | 从结构化结果确定性生成 Markdown |
| 测试 | pytest、pytest-asyncio、respx、Hypothesis、coverage | 单元、状态机、故障注入、HTTP 和性质测试 |
| 工程检查 | Ruff、mypy | 格式化、lint 和严格类型检查 |
| 用户目录 | platformdirs | 跨平台确定配置、数据和报告目录 |

首版不引入 Web 框架、后台任务队列、PostgreSQL、Docker、LangChain Agent、LangGraph、向量数据库或多租户权限系统。

## 3. 总体架构

系统采用单进程、显式状态机和端口／适配器结构：

```text
Typer CLI
  -> Application Service / Review State Machine
      -> Input Providers
          -> PlainDiffProvider
          -> GitHubProvider (PyGithub + limited HTTPX)
          -> GitLabProvider (python-gitlab + limited HTTPX)
      -> Security Boundary
      -> Review Planner / Chunker
      -> ModelGateway
          -> ChatOpenAI
          -> ChatAnthropic
          -> ChatOpenAI(base_url=...) for compatible endpoints
      -> Tool Registry / Deterministic Tools
      -> Finding Validator / Confidence Classifier
      -> SQLite Repositories
      -> Markdown Renderer
```

核心层只依赖项目定义的协议和领域模型，不依赖 Typer、PyGithub、python-gitlab 或 LangChain 的具体对象。第三方对象必须在适配器边界转换成内部类型。

首版网络 I/O 使用 `asyncio`；文件审查单元、工具执行和模型调用保持串行。需求未承诺并发量或处理时长，串行执行更容易证明预算硬约束、checkpoint 粒度和“最多重做一个未完成单元”。

## 4. CLI 与交付

CLI 使用 Typer，Rich 仅负责终端呈现，不承载业务状态。建议命令面如下：

```text
code-review-agent review
code-review-agent resume TASK_ID
code-review-agent status TASK_ID
code-review-agent trace show TRACE_ID
code-review-agent credentials set PROVIDER
code-review-agent credentials clear PROVIDER
code-review-agent providers list
code-review-agent cleanup
```

`review` 一次只接受一种输入。diff 文本可从 stdin 读取，diff 文件使用显式路径，PR/MR 使用 URL。输入冲突在调用任何平台或模型前拒绝。

项目使用 `src/` 布局，以 wheel 和源码包交付。标准复现入口为：

```text
uv sync --locked
uv run code-review-agent --help
```

MVP 支持 Windows 和 Linux。暂不制作独立 EXE，也不把 Docker 作为必需运行方式。

## 5. 输入与平台适配

### 5.1 统一输入模型

三种输入最终转换为内部 `ChangeSet`，至少包含：

- 输入类型和规范化身份；
- 平台、仓库、PR/MR 编号；
- 固定的 base/head SHA；
- 文件旧路径、新路径和变更类型；
- hunk、新旧行号和 patch；
- 二进制、截断、不可读取和未审查标记；
- 输入规则版本与内容摘要。

独立 diff 不伪造提交 SHA。平台输入在获取任务开始时固定 base/head SHA，后续文件内容必须按固定 SHA 读取。

### 5.2 PyGithub 与 python-gitlab 的使用边界

平台接入采用“SDK 优先、必要端点局部 HTTPX”：

- PyGithub 负责 GitHub 身份验证、PR 元数据、文件分页和固定 ref 文件读取；
- python-gitlab 负责 GitLab 身份验证、MR 元数据、diff 分页和固定 ref 文件读取；
- SDK 未及时或完整暴露的 API 字段和端点，通过各 Provider 内的受限 HTTPX 补充；
- 主流程不得接触 SDK 对象、分页对象或异常类型；
- SDK 和 HTTP transport 的自动重试显式关闭；
- 所有重试由应用层决定、记录并计入 checkpoint/trace；
- 用户 URL 只允许精确主机 `github.com` 和 `gitlab.com`；
- API 目标固定为对应官方 API 主机，不从仓库内容读取或修改；
- 不跟随到其他主机的重定向；
- 认证头和凭证不得进入异常文本或 trace。

GitLab 不依赖已弃用的 MR `/changes` 接口，优先使用 `/diffs`，必要时读取 `/raw_diffs`。发现 `collapsed`、`too_large`、`overflow`、缺失页或其他无法取得完整 diff 的状态时，任务在输入阶段失败。

GitHub 必须完整遍历 PR 文件分页。文件数、变更行数或内容大小超过项目上限时拒绝；不能因平台 API 仍允许更多文件而绕过项目的 200 文件、10,000 变更行和 1 MiB 限制。

### 5.3 Diff 解析

unidiff 用于解析文件、hunk、新旧行号、二进制和文件模式等基础结构。项目包装层负责：

- 严格 UTF-8 解码；
- 输入大小、文件数和变更行数上限；
- 空 diff；
- 重命名、纯删除和二进制变更；
- 路径规范化和危险路径拒绝；
- 解析结果与平台元数据一致性；
- 不完整 diff 判定。

不直接把 unidiff 的“解析成功”等同于输入完整或可审查。

## 6. 模型接入

### 6.1 LangChain 使用范围

采用：

- `langchain-core`；
- `langchain-openai` 的 `ChatOpenAI`；
- `langchain-anthropic` 的 `ChatAnthropic`。

不采用：

- LangChain Agent；
- LangGraph 编排或 checkpointer；
- LangSmith 作为必需 trace 后端；
- LangChain 自动工具循环、自动 fallback 或隐式重试。

LangChain 只负责供应商消息和响应适配。预算、重试、安全、trace、结构化校验和恢复由项目自己的 `ModelGateway` 管理。

### 6.2 Provider 能力模型

每个模型配置显式声明：

- provider 类型；
- model ID；
- API base URL；
- 上下文上限；
- 最大输出 token；
- 原生结构化输出能力；
- tool calling 能力；
- 调用前 token counting 能力；
- usage 元数据可信程度。

OpenAI-compatible 端点只承诺兼容标准 Chat Completions 行为。供应商私有的 reasoning 或其他扩展字段不属于 MVP 保证。

结构化评论优先使用 provider 原生 JSON Schema；不可用时使用 tool calling；仅支持基础 Chat Completions 的端点可以使用 JSON 文本 + Pydantic 校验。后一模式的结构可靠性较低，校验失败不得静默修复或自动追加模型调用。

### 6.3 调用状态与重试

每次模型调用使用以下状态：

```text
reserved -> running -> succeeded
                    -> failed_known
                    -> unknown
```

- `reserved`：在 SQLite 事务中预留预算并保存脱敏请求摘要；
- `running`：外部请求已经开始；
- `succeeded`：已取得响应和可靠 usage，按实际值结算；
- `failed_known`：明确失败，记录已知 usage 或保留预留值；
- `unknown`：请求可能已被供应商处理，但本地未取得确定结果。

LangChain provider 的自动重试设为 0。MVP 不使用流式输出。`unknown` 不自动重试，按预留最大 token 计入不确定消耗并暂停，直到用户显式执行 `resume`。

任务固定 provider、model、prompt/规则版本和输入版本。恢复时不得自动切换模型或供应商。

## 7. 预算控制

预算账本与任务状态保存在同一 SQLite 数据库。每次模型调用前，在一个事务中完成：

1. 计算输入 token 或安全上界；
2. 加上受控的最大输出 token；
3. 检查剩余预算；
4. 写入预留流水和调用记录；
5. 允许或拒绝调用。

优先使用供应商提供的可靠 token counting。无法可靠预计算时，使用完整序列化请求的 UTF-8 字节长度加协议余量作为保守上界，并要求端点遵守最大输出 token 参数。若端点不能满足这两点，则标记为不兼容硬预算模式，不允许执行审查。

成功调用使用实际 usage 结算。缺失或不可信 usage、连接中断及结果不明调用保留预留值。失败调用和显式重试均产生独立预算流水，不覆盖历史消耗。

## 8. SQLite 持久化

使用标准库 `sqlite3`，不引入 ORM。数据库至少包含：

- `tasks`：任务身份、固定输入、规则版本、状态和保留时间；
- `work_units`：输入、文件审查、工具执行和报告单元；
- `model_calls`：调用状态、provider、model、请求／响应 trace 关联；
- `budget_ledger`：预留、实际、不确定、释放和追加预算流水；
- `tool_calls`：工具、声明版本、输入摘要、输出和状态；
- `findings`：位置、问题、证据、影响、建议、置信度和 trace ID；
- `trace_events`：脱敏过程事件、错误和 checkpoint 事件；
- `reports`：报告路径、版本和写入状态。

启用外键、事务和合理的 `busy_timeout`。首版单写入进程，WAL 用于降低状态查询与写入冲突。schema 通过项目内版本化 SQL migration 和 `PRAGMA user_version` 管理。

报告文件使用“临时文件写入完成后原子替换”。恢复时按任务 ID 更新该任务报告，不覆盖其他任务。

SQLite 不做应用层加密。当前威胁模型是同一可信 OS 账号；数据库只保存脱敏材料，并依赖用户目录文件权限。未来如果增加跨账号共享或磁盘泄露防护，再评估 SQLCipher。

## 9. 安全与凭证

### 9.1 凭证

GitHub、GitLab、OpenAI、Anthropic 和 OpenAI-compatible 凭证保存在 keyring。配置文件只保存凭证别名，不保存明文。

允许环境变量作为 CI 或无交互环境的显式注入方式，但环境变量不写回数据库、报告或配置。若当前系统没有安全 keyring backend，交互式 `credentials set` 明确失败，不回退到明文文件。

### 9.2 统一安全边界

以下内容在发送给模型或写入持久存储前统一检查：

- diff 的新增、删除和上下文行；
- 按需读取的仓库文本；
- 工具输入与输出；
- prompt；
- 模型响应；
- 错误消息；
- Markdown 报告和 trace 事件。

detect-secrets 只启用项目固定的内置 detector，关闭联网验证，不读取目标仓库 baseline、allowlist、插件或过滤器。项目自有规则补充个人信息、连接串、证书及显式业务敏感标记。

处理结果分为：安全、已脱敏、必须跳过。无法安全判断时按敏感处理；跳过内容必须降低覆盖状态，不能输出完整审查结论。

不保存未脱敏原始输入。访问凭证永远不进入领域模型和 trace。

### 9.3 仓库执行边界

系统不运行目标仓库中的：

- 脚本、测试、构建和 typecheck；
- 包管理器、Git hook 和外部 diff；
- 插件、依赖和可执行配置；
- shell 命令或仓库声明的远程工具。

仓库中的代码、注释、Markdown、配置和 prompt injection 文本只作为不可信数据，不得改变 provider、预算、工具权限、发布目标或安全规则。

## 10. 工具扩展

工具通过 Python packaging entry point group `code_review_agent.tools` 注册。工具实现由维护者安装，绝不从受审仓库动态加载。

每个工具声明：

- 唯一名称和版本；
- 支持语言；
- 输入／输出 Pydantic schema；
- 所需能力；
- 是否确定性；
- 超时和最大输入／输出；
- 失败对置信度和覆盖范围的影响。

注册器只允许 MVP 能力 `read_authorized_text` 和 `deterministic_analysis`。工具不能访问网络、凭证、SQLite 连接或通用文件系统路径。

首个验收工具为静态危险模式检查器：对传入的已授权文本和变更行执行项目内置规则，不调用 shell，不加载仓库配置。增加第二个工具时只新增实现和 entry-point 声明，主编排代码保持不变，以此验收 FR-017。

## 11. 评论、置信度与报告

模型输出先经 Pydantic schema 校验，再执行确定性校验：

- 路径必须来自 `ChangeSet`；
- 行号必须能映射到相应新行、旧行或明确文件级位置；
- 证据必须来自实际读取的 diff、上下文或工具结果；
- 只保留本次变更新增或明显加剧的问题；
- 重复发现按稳定 fingerprint 合并；
- 严重程度和置信度分别存储。

高置信度要求直接代码证据，并满足以下之一：

- 确定性工具验证；
- 可直接核对的语言语义依据。

证据不足或冲突时降为“仅供参考”；完全没有可行动依据时不输出。

Jinja2 只把结构化结果渲染为 Markdown，不在模板中执行业务判断。报告展示审查对象、固定版本、覆盖范围、完成状态、评论、置信度依据、预算、未审查内容和 trace ID。

## 12. 测试与工程验证

最低工程命令：

```text
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

测试分层：

1. 领域单元测试：状态转换、预算账本、置信度和结果合并；
2. Diff 测试：正常、空、纯删除、重命名、二进制、损坏和三类超限；
3. Provider 合约测试：分页、固定 SHA、认证失败、Draft、关闭／合并、截断和平台错误；
4. 模型适配测试：OpenAI、Anthropic、OpenAI-compatible 的结构化输出、usage 缺失和异常映射；
5. 故障注入测试：在预算预留、外部调用、响应持久化和报告替换各位置中断；
6. 安全测试：新增／删除行 secret、上下文回显、工具回显、模型回显、提示注入和恶意路径；
7. 固定质量样例：至少 8 个，覆盖三种语言、缺陷、干净变更、恢复、预算和安全场景；
8. 性质测试：随机状态序列不得超预算、已完成单元不得倒退、不同任务报告不得互相覆盖。

平台和模型测试默认使用 fake/recorded fixture，不要求真实凭证。真实 GitHub/GitLab 私有测试仓库和模型调用作为手动验收，不进入默认测试套件。

## 13. 被拒绝或暂缓的方案

### LangGraph

暂不采用。它能保存 graph step，但外部调用在完成记录前中断时仍需要应用自行处理幂等和未知结果。本项目必须显式展示 `unknown` 调用、预算预留和人工恢复，使用自有状态机更直接。

### LiteLLM

暂不采用。其统一路由、fallback、重试和成本能力与项目自有预算及恢复机制重叠。当前只需 OpenAI、Anthropic 和标准 OpenAI-compatible，LangChain provider adapters 已足够。

### 各供应商原生模型 SDK

暂不直接使用。控制力最强，但需要自行统一消息、工具调用、结构化输出、usage 和错误。模型能力通过自有 `ModelGateway` 隔离，未来如 LangChain 丢失必需元数据，可在单个 adapter 内替换为原生 SDK。

### 完全手写 GitHub/GitLab REST 客户端

暂不采用。虽然底层控制最强，但认证、分页、编码和错误处理开发量较大。最终选择 SDK 优先，并把不完整端点限制在 Provider 内用 HTTPX 补齐。

### SQLAlchemy/Alembic

暂不采用。单机 SQLite、固定 schema 和单写入进程不需要 ORM；直接事务更便于验证预算与 checkpoint 原子性。

### PostgreSQL、Redis 和任务队列

暂不采用。MVP 无并发和服务化要求。若未来转为多用户服务，再重新评估数据库、队列、租约和分布式幂等。

### Semgrep、typecheck 和仓库测试

暂不纳入运行时。它们可能加载仓库配置、插件、依赖或执行受仓库影响的行为，与首版执行边界冲突。MVP 使用只读、确定性的内置文本分析工具。

## 14. 已知风险与验证优先级

正式铺开实现前，优先完成三个最小风险验证：

1. SQLite 中断实验：证明已完成单元复用、最多重做一个未完成单元、`unknown` 调用不自动重试；
2. 预算实验：证明预留、实际结算、不确定消耗、预算耗尽和追加预算行为；
3. 安全实验：证明所有模型出站和本地落盘路径都经过相同脱敏边界。

随后验证 GitHub/GitLab diff 完整性和固定版本读取。任何平台无法证明获得完整 diff 时，宁可明确失败，不生成看似完整的报告。

## 15. 结论

最终路线是：**Python 3.12 单机 CLI + LangChain 模型适配器 + 自有显式状态机 + SQLite + SDK 封装的平台 Provider + 统一安全边界**。

该方案优先满足可恢复、可追溯、硬预算和安全边界，同时利用成熟 SDK 降低平台及模型接入成本。首版保持单进程、串行执行和有限扩展能力，不为尚未确认的远程回评、并发服务化或任意插件生态提前增加复杂度。
