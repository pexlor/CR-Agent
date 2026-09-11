# Code Review Agent 开发实施计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `subagent-driven-development`（推荐）或 `executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）跟踪进度。每次只执行一个任务；未通过该任务的测试和审查，不得进入下一任务。

**目标：** 从零实现一个本地单进程 Python CLI Code Review Agent，支持 unified diff、GitHub PR、GitLab MR 三种输入，输出可恢复、可追溯、受 token 预算约束且经过安全处理的 Markdown 审查报告。

**架构：** 使用模块化单体、端口/适配器和显式阶段机。领域层只表达任务、安全、输入、规划、执行、发现、预算、Trace 和报告规则；应用层编排流程；SQLite、平台 SDK、模型 SDK、keyring、Jinja2 和文件系统全部位于适配器层。开发采用纵向增量方式，先建立可验证的领域与事务基础，再打通本地 diff 最小闭环，随后补齐恢复、平台输入和完整验收。

**技术栈：** Python 3.12、uv、Pydantic v2、Typer、Rich、sqlite3、unidiff、LangChain ChatModel adapters、PyGithub、python-gitlab、HTTPX、keyring、detect-secrets、Jinja2、pytest、Hypothesis、Ruff、mypy。

---

## 1. 开发原则

1. 每个 Agent 一次只接收一个任务，不允许同时开发多个模块。
2. 每个任务必须按“失败测试 → 最小实现 → 测试通过 → 全量检查 → 代码审查 → commit”执行。
3. 核心层不得导入 Typer、Rich、sqlite3、LangChain、PyGithub、python-gitlab、HTTPX、keyring 或 Jinja2。
4. 所有外部返回均按不可信数据处理；未经安全处理的 diff、上下文、工具结果、模型响应和错误不得持久化。
5. 所有模型调用必须经过安全检查、预算预留、Trace 和调用状态记录。
6. CandidateFinding 不能直接进入报告，必须经过确定性发现处理。
7. 不执行受审仓库中的脚本、测试、构建、typecheck、hook、插件、依赖或配置。
8. 一个任务完成前，不修改与该任务无关的文件。
9. 每次提交保持可运行、可测试；提交信息使用 Conventional Commits。
10. 当前设计文档存在未提交修改，开始编码前先提交或明确纳入开发分支，避免实现依据漂移。
11. 每完成一个任务清单中的小步骤（测试写完并运行、实现写完并测试通过、审查通过等）
    就立即提交一次，不要积累多个步骤后一次性提交。提交粒度以任务清单中的每个
    `- [ ]` 条目为单位；同一条目内的多个文件改动算一次提交，跨条目不合并提交。
    这样任何时刻中断都能从最近一次成功状态恢复，且每次 `git log` 都能看清单个
    步骤做了什么、为什么这么做。

## 2. 目标目录结构

```text
.
├── pyproject.toml
├── uv.lock
├── README.md
├── config.example.toml
├── src/code_review_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── bootstrap.py
│   ├── config.py
│   ├── cli/
│   │   ├── app.py
│   │   ├── commands.py
│   │   ├── presenters.py
│   │   └── exit_codes.py
│   ├── application/
│   │   ├── dto.py
│   │   ├── orchestration.py
│   │   ├── task_service.py
│   │   ├── credential_service.py
│   │   └── query_service.py
│   ├── domain/
│   │   ├── common/
│   │   │   ├── errors.py
│   │   │   ├── identifiers.py
│   │   │   ├── digests.py
│   │   │   └── time.py
│   │   ├── security/
│   │   ├── task/
│   │   ├── input/
│   │   ├── planning/
│   │   ├── execution/
│   │   ├── findings/
│   │   ├── budget/
│   │   ├── trace/
│   │   └── report/
│   ├── ports/
│   │   ├── persistence.py
│   │   ├── security.py
│   │   ├── input.py
│   │   ├── model.py
│   │   ├── tools.py
│   │   ├── output.py
│   │   ├── credentials.py
│   │   └── clock.py
│   ├── adapters/
│   │   ├── sqlite/
│   │   │   ├── connection.py
│   │   │   ├── migrations.py
│   │   │   ├── repositories.py
│   │   │   └── unit_of_work.py
│   │   ├── input/
│   │   │   ├── plain_diff.py
│   │   │   ├── github.py
│   │   │   └── gitlab.py
│   │   ├── model/
│   │   │   ├── gateway.py
│   │   │   ├── openai.py
│   │   │   └── anthropic.py
│   │   ├── security/
│   │   │   ├── scanner.py
│   │   │   └── logging.py
│   │   ├── tools/
│   │   │   ├── registry.py
│   │   │   └── runtime.py
│   │   ├── output/
│   │   │   └── markdown.py
│   │   └── credentials/
│   │       └── keyring_store.py
│   ├── resources/
│   │   ├── security_policy.toml
│   │   ├── tools/
│   │   └── templates/review.md.j2
│   └── migrations/
│       └── 0001_initial.sql
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── architecture/
│   ├── acceptance/
│   ├── fixtures/
│   └── golden/
└── evidence/
    ├── test-runs/
    ├── reports/
    └── ai-collaboration/
```

目录可以在实现中继续按职责细分，但不得把多个领域模块合并成单个大文件，也不得创建通用 `utils.py`、`helpers.py` 或 `managers.py`。

## 3. 标准 Agent 任务模板

每次委派都使用以下模板：

```text
任务：<只写一个可独立验收的目标>

依据：
- <对应设计文档路径和章节>
- <对应 FR/AC 编号>

允许修改：
- <精确文件列表>

禁止修改：
- 其他模块公共契约
- 无关设计文档
- 已冻结的输入、预算、安全和恢复语义

执行要求：
1. 先阅读依据和现有代码。
2. 先写失败测试并运行，记录预期失败原因；测试写完、运行确认失败后立即提交
   （例如 `test: add failing test for <behavior>`）。
3. 编写满足测试的最小实现；实现写完、测试转为通过后立即提交
   （例如 `feat: implement <behavior>`）。
4. 运行任务级测试、Ruff 和 mypy；若因此修复了实现或测试，修复完成后立即提交。
5. 检查没有 secret、裸第三方异常或跨层依赖；发现问题修复后立即提交。
6. 输出变更摘要、命令及实际结果、剩余风险。
7. 等待审查通过后，提交任务收尾变更（如更新任务清单勾选状态）。

提交纪律：
- 每个清单步骤只做一次提交，不把多个步骤的改动积压到一起提交。
- 不为尚未验证通过的代码提交（测试红灯状态下的测试代码本身除外，其提交信息
  必须能看出这是"先写失败测试"步骤，例如 `test:` 前缀且描述预期失败原因）。
- 提交信息使用 Conventional Commits（`test:`/`feat:`/`fix:`/`refactor:`/`docs:`/
  `chore:`），并在正文中简要说明属于任务清单的哪个步骤。

完成标准：
- 指定测试通过。
- 不存在跳过测试或 TODO 核心逻辑。
- 公开契约和稳定错误码与设计一致。
- 变更可由下一任务直接复用。
```

## 4. 阶段总览

```text
M0 设计基线与工程骨架
  → M1 公共契约、安全证明、Trace 信封
  → M2 任务/预算领域状态机
  → M3 SQLite schema 与统一 UoW
  → M4 本地 diff 输入闭环
  → M5 规划与声明式工具
  → M6 模型网关与工作单元执行
  → M7 发现收敛与结果快照
  → M8 Markdown 报告与最小 review CLI
  → M9 checkpoint、暂停、恢复与 unknown
  → M10 GitHub/GitLab 和凭证
  → M11 完整 CLI、清理与查询
  → M12 固定样例、故障注入和交付
```

任何阶段都必须保持：

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

## 5. 逐步实施任务

### 任务 0：冻结设计基线

**依据：** `docs/需求拆解.md`、`docs/验收清单.md`、全部模块设计。

**文件：**
- 修改（仅文档状态字段，且仅在确认内容已完成的前提下）：
  - `docs/模块设计/CLI接口模块.md`
  - `docs/模块设计/任务模块.md`
  - `docs/模块设计/输入模块.md`
  - `docs/模块设计/审查规划模块.md`
  - `docs/模块设计/审查执行模块.md`
  - `docs/模块设计/发现处理模块.md`
  - `docs/模块设计/预算模块.md`
  - `docs/模块设计/安全模块.md`
  - `docs/模块设计/Trace模块.md`
  - `docs/模块设计/报告模块.md`
  - `docs/模块设计/审查编排模块.md`
  - `docs/模块设计/扩展与适配器模块.md`
  - `docs/模块设计/基础设施模块.md`
- 创建：`docs/实现决策.md`

- [ ] 核对 27 条 P0 需求、14 组固定样例和模块设计是否一致。
- [ ] 明确架构设计中“Review Tool 只允许声明式规则包”覆盖技术选型中的早期 entry point 方案。
- [ ] 记录首个 schema 版本、规则版本、工具 contract 版本和报告模板版本。
- [ ] 记录实现期间不得变更的固定裁决。
- [ ] 运行文档关键词检查，确保没有影响实现的“待确认”“TODO”“后续决定”。
- [ ] 提交：

```bash
git add docs
git commit -m "docs: freeze implementation baseline"
```

**门禁：** 设计基线未冻结时不得开始编码。

### 任务 1：初始化 Python 工程与质量门禁

**文件：**
- 创建：`pyproject.toml`
- 创建：`src/code_review_agent/__init__.py`
- 创建：`src/code_review_agent/__main__.py`
- 创建：`tests/unit/test_package.py`
- 创建：`tests/architecture/test_dependencies.py`
- 创建：`.gitignore`

- [ ] 先写测试，验证包可导入、版本非空、CLI 模块尚不产生副作用。
- [ ] 配置 Python 3.12、uv、pytest、coverage、Ruff 和严格 mypy。
- [ ] 配置 `code-review-agent = "code_review_agent.cli.app:main"`，入口可暂时返回稳定的未实现错误。
- [ ] 增加架构测试，禁止 `domain` 导入 `typer`、`rich`、`sqlite3`、`langchain*`、
      `github`（PyGithub）、`gitlab`（python-gitlab）、`httpx`、`keyring`、`jinja2`
      等第三方适配器库；对 `src/code_review_agent/domain` 下所有模块的导入语句做静态
      扫描，命中上述任一模块名即判定失败，防止后续任务在领域层引入具体适配器依赖。
- [ ] 运行：

```bash
uv lock
uv sync --locked
uv run pytest tests/unit/test_package.py tests/architecture/test_dependencies.py -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
```

- [ ] 提交：

```bash
git add pyproject.toml uv.lock .gitignore src tests
git commit -m "build: initialize python project"
```

### 任务 2：实现公共值对象与稳定错误

**文件：**
- 创建：`src/code_review_agent/domain/common/errors.py`
- 创建：`src/code_review_agent/domain/common/identifiers.py`
- 创建：`src/code_review_agent/domain/common/digests.py`
- 创建：`src/code_review_agent/domain/common/time.py`
- 创建：`tests/unit/domain/common/`

- [ ] 测试 UUIDv4 规范小写格式、UTC 时间校验、规范 JSON 和 SHA-256 稳定性。
- [ ] 测试错误只包含稳定 code、category、stage、recoverable、next actions 和安全标量 details。
- [ ] 实现 `TaskId` 等强类型标识、注入式 `Clock`、规范序列化和摘要函数。
- [ ] 禁止错误对象保存第三方异常正文、请求体、响应体或凭证。
- [ ] 运行公共领域测试和性质测试。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/common tests/unit/domain/common
git commit -m "feat: add common domain contracts"
```

### 任务 3：实现安全领域核心

**依据：** `docs/模块设计/安全模块.md`，FR-025～FR-028。

**文件：**
- 创建：`src/code_review_agent/domain/security/models.py`
- 创建：`src/code_review_agent/domain/security/policy.py`
- 创建：`src/code_review_agent/domain/security/service.py`
- 创建：`src/code_review_agent/ports/security.py`
- 创建：`tests/unit/domain/security/`
- 创建：`tests/contract/test_security_scanner_contract.py`

- [ ] 测试 `safe/redacted/blocked/indeterminate` 四种决策和 fail-closed 行为。
- [ ] 测试来源、用途、策略版本、摘要和 provenance 不可伪造或跨用途复用。
- [ ] 测试 `<REDACTED:CATEGORY:NN>` 占位符稳定且不含原值特征。
- [ ] 测试 diff 脱敏保持行数、行类型和行号结构。
- [ ] 实现 `PreparedSanitizedArtifact`，事务提交前不得提升为 `SanitizedArtifactRef`。
- [ ] 实现策略兼容和非降级判断。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/security src/code_review_agent/ports/security.py tests
git commit -m "feat: implement security domain boundary"
```

### 任务 4：实现 Trace 领域契约

**依据：** `docs/模块设计/Trace模块.md`，FR-015～FR-016。

**文件：**
- 创建：`src/code_review_agent/domain/trace/models.py`
- 创建：`src/code_review_agent/domain/trace/schemas.py`
- 创建：`src/code_review_agent/domain/trace/service.py`
- 创建：`tests/unit/domain/trace/`

- [ ] 测试任务内 sequence、幂等键、同任务边和 append-only 规则。
- [ ] 测试工具未执行不能生成 succeeded，模型 unknown 不能生成 succeeded。
- [ ] 测试每个最终 finding 恰好有一个当前有效 TraceLink，且 direct evidence 非空。
- [ ] 实现标准事件类型与版本化 schema 注册表。
- [ ] 实现 TraceMutationIntent 和只读投影，不实现数据库写入。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/trace tests/unit/domain/trace
git commit -m "feat: add trace event contracts"
```

### 任务 5：实现预算领域状态机

**依据：** `docs/模块设计/预算模块.md`，FR-020～FR-022。

**文件：**
- 创建：`src/code_review_agent/domain/budget/models.py`
- 创建：`src/code_review_agent/domain/budget/ledger.py`
- 创建：`src/code_review_agent/domain/budget/service.py`
- 创建：`tests/unit/domain/budget/`

- [ ] 用决策表测试授权、活动预留、已知消耗、不确定消耗、超额、赤字和余额公式。
- [ ] 测试每个 model call 只能有一个 reservation，重试必须新建 reservation。
- [ ] 测试普通预算不足不冻结账户，actual usage 超预留必须冻结。
- [ ] 测试追加授权不清零历史、不自动解冻；解冻必须显式恢复和能力重验证。
- [ ] 用 Hypothesis 验证任意合法账本序列不会产生负 token 字段或重复结算。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/budget tests/unit/domain/budget
git commit -m "feat: implement token budget ledger"
```

### 任务 6：实现任务聚合、租约与 checkpoint

**依据：** `docs/模块设计/任务模块.md`，FR-011～FR-014。

**文件：**
- 创建：`src/code_review_agent/domain/task/models.py`
- 创建：`src/code_review_agent/domain/task/state_machine.py`
- 创建：`src/code_review_agent/domain/task/service.py`
- 创建：`tests/unit/domain/task/`

- [ ] 测试控制状态、阶段、结果状态和交付状态四者正交。
- [ ] 测试阶段只能前进，ResultSnapshot 不可修改，InputBinding 只能写入一次。
- [ ] 测试 expected version、lease ID 和 fencing token 任一不匹配时整体拒绝。
- [ ] 测试租约 120 秒、30 秒续约规则和旧执行者不能迟到提交。
- [ ] 测试 checkpoint sequence、前驱和“最多重做一个未完成单元”判定。
- [ ] 测试创建、暂停、终止、恢复、预算异常、unknown 和报告重试迁移。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/task tests/unit/domain/task
git commit -m "feat: implement task lifecycle"
```

### 任务 7：建立 SQLite schema、Repository 和统一 UoW

**依据：** `docs/模块设计/基础设施模块.md`。

**文件：**
- 创建：`src/code_review_agent/migrations/0001_initial.sql`
- 创建：`src/code_review_agent/adapters/sqlite/connection.py`
- 创建：`src/code_review_agent/adapters/sqlite/migrations.py`
- 创建：`src/code_review_agent/adapters/sqlite/repositories.py`
- 创建：`src/code_review_agent/adapters/sqlite/unit_of_work.py`
- 创建：`tests/integration/sqlite/`

- [ ] 先为 PRAGMA、迁移校验和、FK、CHECK、unique 和索引写失败测试。
- [ ] 实现完整逻辑表，不把大对象塞入单个可变 JSON。
- [ ] UoW 只接收类型化 Intent，使用 `BEGIN IMMEDIATE` 和参数绑定。
- [ ] 在同一事务核对任务版本、租约、fencing、预算 revision、安全证明、Trace sequence 和 checkpoint。
- [ ] 本任务实现事务框架及任务/安全/Trace/预算基础 Intent；后续任务接入新的领域
      Intent 时必须补充对应拓扑写入和故障注入测试，禁止退化为任意 SQL 接口。
- [ ] 对每个写入阶段做故障注入，证明失败全部 rollback。
- [ ] 用双连接测试旧 fencing 和版本冲突。
- [ ] 提交：

```bash
git add src/code_review_agent/migrations src/code_review_agent/adapters/sqlite tests/integration/sqlite
git commit -m "feat: add sqlite persistence unit of work"
```

**门禁：** UoW 故障注入未通过时，不得接入真实外部 I/O。

### 任务 8：实现 plain diff 输入

**依据：** `docs/模块设计/输入模块.md`，FR-001、FR-003～FR-004。

**文件：**
- 创建：`src/code_review_agent/domain/input/models.py`
- 创建：`src/code_review_agent/domain/input/service.py`
- 创建：`src/code_review_agent/ports/input.py`
- 创建：`src/code_review_agent/adapters/input/plain_diff.py`
- 创建：`tests/unit/domain/input/`
- 创建：`tests/contract/test_plain_diff_provider.py`
- 创建：`tests/fixtures/diffs/`

- [ ] 测试文本与文件的相同内容产生相同身份。
- [ ] 测试空 diff、纯删除、重命名、二进制、损坏 UTF-8 和危险路径。
- [ ] 分别测试 1 MiB、200 文件和 10,000 变更行上限。
- [ ] 实现严格结构解析、安全处理、CompletenessProof、ChangeSet 和 InputBinding。
- [ ] 原始 diff 不进入数据库、错误或测试快照；夹具只使用模拟内容。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/input src/code_review_agent/ports/input.py src/code_review_agent/adapters/input tests
git commit -m "feat: support unified diff input"
```

### 任务 9：实现审查规划器

**依据：** `docs/模块设计/审查规划模块.md`。

**文件：**
- 创建：`src/code_review_agent/domain/planning/models.py`
- 创建：`src/code_review_agent/domain/planning/planner.py`
- 创建：`tests/unit/domain/planning/`

- [ ] 测试文件优先、hunk 装箱、超大 hunk 行块切分。
- [ ] 测试 planned scope 恰好属于一个 WorkUnit，unreviewable 不进入执行。
- [ ] 测试 execution rank 变化不改变 work unit identity。
- [ ] 测试相同固定输入、策略、容量和工具目录产生相同 plan fingerprint。
- [ ] 测试恢复时摘要不一致拒绝，不能静默重新规划。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/planning tests/unit/domain/planning
git commit -m "feat: add deterministic review planner"
```

### 任务 10：实现声明式工具 Registry 与受限运行时

**依据：** `docs/模块设计/扩展与适配器模块.md`，FR-017～FR-019。

**文件：**
- 创建：`src/code_review_agent/adapters/tools/registry.py`
- 创建：`src/code_review_agent/adapters/tools/runtime.py`
- 创建：`src/code_review_agent/resources/tools/builtin.toml`
- 创建：`src/code_review_agent/ports/tools.py`
- 创建：`tests/unit/tools/`
- 创建：`tests/contract/test_tool_extension.py`

- [ ] 测试允许的有限 op 和所有禁止能力。
- [ ] 测试未知字段、重复身份、任意 URL、Python 对象路径、shell、glob、regex、环境变量和仓库路径均被拒绝。
- [ ] 测试相同规则/输入产生相同摘要；超限时整体失败，不返回截断成功。
- [ ] 新增一个静态模式检查工具，只修改声明资源，不修改编排代码。
- [ ] 记录接入前后 diff 作为 FR-017 证据。
- [ ] 提交：

```bash
git add src/code_review_agent/adapters/tools src/code_review_agent/resources/tools src/code_review_agent/ports/tools.py tests
git commit -m "feat: add restricted declarative tools"
```

### 任务 11：实现模型网关和 fake provider

**依据：** `docs/模块设计/审查执行模块.md`、`docs/技术选型.md`。

**文件：**
- 创建：`src/code_review_agent/domain/execution/models.py`
- 创建：`src/code_review_agent/ports/model.py`
- 创建：`src/code_review_agent/adapters/model/gateway.py`
- 创建：`tests/fakes/model_provider.py`
- 创建：`tests/unit/domain/execution/test_model_states.py`
- 创建：`tests/contract/test_model_gateway.py`

- [ ] 测试 provider state 与 response state 两轴组合。
- [ ] 测试 JSON Schema、tool calling、JSON text 三种固定结构化策略。
- [ ] 测试禁用 streaming、retry、fallback 和动态工具调用。
- [ ] 测试发送前失败释放预留，发送后不明转 unknown。
- [ ] 测试 usage missing/untrusted、actual 小于预留和 actual 大于预留。
- [ ] `tests/fakes/model_provider.py` 中的 fake provider 必须实现与任务 20 真实
      OpenAI/Anthropic 适配器相同的 `ModelGatewayPort` 契约（同一组抽象方法签名、
      同样的 provider_state/response_state 两轴语义），作为后续契约测试的参照基线；
      任务 20 交付时须运行与本任务相同的契约测试套件验证真实适配器行为一致。
- [ ] 先只接 fake provider，真实 OpenAI/Anthropic 适配放到任务 20（不是任务 18，
      任务 18 是暂停/恢复/unknown，不引入真实模型 SDK）。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/execution src/code_review_agent/ports/model.py src/code_review_agent/adapters/model/gateway.py tests
git commit -m "feat: add model gateway contracts"
```

### 任务 12：实现工作单元执行管线

**文件：**
- 创建：`src/code_review_agent/domain/execution/executor.py`
- 创建：`tests/unit/domain/execution/test_executor.py`
- 创建：`tests/integration/test_execution_uow.py`

- [ ] 测试固定 WorkUnit → 工具 → PromptEnvelope → 模型 → CandidateFinding 的顺序。
- [ ] 测试一次 execution 至多一个模型调用，显式重试必须创建新 execution。
- [ ] 明确并测试“可选工具失败可继续但降级证据”与“影响覆盖的工具失败使单元
      partial”两类判定：按 `docs/模块设计/审查执行模块.md` 中 `ToolSelection.failure_impact`
      的 `evidence_degraded`（继续执行、标记证据降级）与 `coverage_degraded`（终止该单元、
      标记覆盖降级）两个取值区分，不能笼统当作同一种“继续但降级”处理。
- [ ] 测试模型调用准备事务、running 标记和完成事务。
- [ ] 在每个边界注入崩溃，验证 running 无完成事务会转 unknown。
- [ ] 测试安全响应被拒绝时仍按可信 usage 结算，但不生成候选。
- [ ] 测试 CandidateFinding 只能引用本次执行实际材料。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/execution tests
git commit -m "feat: execute review work units"
```

### 任务 13：实现发现处理和置信度

**依据：** `docs/模块设计/发现处理模块.md`，FR-005～FR-007、FR-023～FR-024。

**文件：**
- 创建：`src/code_review_agent/domain/findings/models.py`
- 创建：`src/code_review_agent/domain/findings/processor.py`
- 创建：`tests/unit/domain/findings/`

- [ ] 测试新行、旧行、文件级、重命名和跨文件定位。
- [ ] 测试 direct evidence、工具证据、语言语义和冲突处理。
- [ ] 测试 pre-existing、unrelated、not-actionable 和无直接证据候选被拒绝。
- [ ] 测试语义 fingerprint、恢复重排和去重幂等。
- [ ] 穷举 high/advisory 置信度决策表，确认模型自评分不参与裁决。
- [ ] 测试 CoverageSnapshot 的 reviewed/unreviewed/unreviewable/unknown 守恒。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/findings tests/unit/domain/findings
git commit -m "feat: validate and consolidate findings"
```

### 任务 14：实现 ResultSnapshot 和报告模型

**文件：**
- 创建：`src/code_review_agent/domain/report/models.py`
- 创建：`src/code_review_agent/domain/report/builder.py`
- 创建：`tests/unit/domain/report/test_builder.py`

- [ ] 测试 no_changes、complete_no_findings、complete_with_findings、partial、failed、unknown 六种状态。
- [ ] 测试快照固定 InputBinding、ReviewPlan、CoverageSnapshot、FindingSet、BudgetSummary、UnknownAttemptSet 和 TraceSummary。
- [ ] 测试引用缺失、跨任务、摘要不符和 schema 不支持时失败。
- [ ] 测试 CandidateFinding 不能进入 ReportModel。
- [ ] 测试“未发现有效问题”不被表述为不存在缺陷。
- [ ] 提交：

```bash
git add src/code_review_agent/domain/report tests/unit/domain/report
git commit -m "feat: build report model from snapshots"
```

### 任务 15：实现 Markdown 渲染和原子交付

**文件：**
- 创建：`src/code_review_agent/resources/templates/review.md.j2`
- 创建：`src/code_review_agent/adapters/output/markdown.py`
- 创建：`src/code_review_agent/ports/output.py`
- 创建：`tests/golden/review/`
- 创建：`tests/integration/test_markdown_delivery.py`

- [ ] 为六种结果状态建立 golden tests。
- [ ] 测试模板只展示字段，不重新计算状态、预算、排序或置信度。
- [ ] 测试完整 UTF-8 字节安全复扫后才允许写入。
- [ ] 测试临时文件、文件 `fsync`、`os.replace`、父目录 `fsync` 和摘要核对。
- [ ] 在 replace 前后、数据库终态提交前注入崩溃，验证 unknown 核对。
- [ ] 测试不同任务不会覆盖，同任务新快照原子替换固定路径。
- [ ] 提交：

```bash
git add src/code_review_agent/resources/templates src/code_review_agent/adapters/output src/code_review_agent/ports/output.py tests
git commit -m "feat: deliver markdown review reports"
```

### 任务 16：实现应用编排和本地 diff 最小闭环

**依据：** `docs/模块设计/审查编排模块.md`。

**文件：**
- 创建：`src/code_review_agent/application/dto.py`
- 创建：`src/code_review_agent/application/orchestration.py`
- 创建：`src/code_review_agent/application/task_service.py`
- 创建：`src/code_review_agent/application/query_service.py`
- 创建：`tests/integration/test_plain_diff_review_flow.py`

- [ ] 用 fake scanner、fake model 和内存/SQLite 适配器先写端到端失败测试。
- [ ] 实现单调阶段机和 ExecutionSession。
- [ ] 打通创建任务、输入、规划、执行、收敛、快照、报告和交付。
- [ ] 提供任务状态和评论 Trace 的只读查询服务，查询不得续约或改变任务状态。
- [ ] 测试预算不足、安全跳过、工具失败、模型输出无效时仍能诚实收敛。
- [ ] 测试停止后不再产生外部调用。
- [ ] 生成第一份真实 Markdown 测试报告。
- [ ] 提交：

```bash
git add src/code_review_agent/application tests/integration
git commit -m "feat: complete local diff review flow"
```

**里程碑：** 至此应能使用 fake model 完成本地 diff → Markdown 报告闭环。

### 任务 17：实现 review/status/trace 最小 CLI

**文件：**
- 创建：`src/code_review_agent/cli/app.py`
- 创建：`src/code_review_agent/cli/commands.py`
- 创建：`src/code_review_agent/cli/presenters.py`
- 创建：`src/code_review_agent/cli/exit_codes.py`
- 创建：`src/code_review_agent/bootstrap.py`
- 创建：`src/code_review_agent/config.py`
- 创建：`tests/integration/cli/`

- [ ] 测试 `--diff-file/--stdin/--url` 恰好一个、TTY 行为和预算范围。
- [ ] 测试 stdout 只输出最终结果，stderr 输出进度/警告/错误。
- [ ] 测试 human 与 `--json` 共享 DTO 和退出码。
- [ ] 测试 CLI 不导入 repository、SQLite、SDK、keyring 或领域具体实现。
- [ ] 打通本地 diff 的 `review`、`status` 和 `trace show`。
- [ ] 提交：

```bash
git add src/code_review_agent/cli src/code_review_agent/bootstrap.py src/code_review_agent/config.py tests
git commit -m "feat: add initial cli commands"
```

### 任务 18：实现暂停、恢复、unknown 和预算追加

**文件：**
- 修改：`src/code_review_agent/application/orchestration.py`
- 修改：`src/code_review_agent/application/task_service.py`
- 修改：`src/code_review_agent/cli/commands.py`
- 创建：`tests/integration/recovery/`

- [ ] 在输入、工具、模型 running、发现收敛、报告 replace 后等固定位置注入中断。
- [ ] 测试完成 checkpoint 复用，最多重做一个未完成本地单元。
- [ ] 测试 running 模型调用恢复为 unknown，未确认时不重试。
- [ ] 测试 `--confirm-unknown-retry` 创建新 execution/call/reservation/Trace。
- [ ] 测试追加预算幂等、历史消耗不清零、冻结不自动解除。
- [ ] 测试 SIGINT/SIGTERM 协作式暂停行为。
- [ ] 提交：

```bash
git add src/code_review_agent/application src/code_review_agent/cli tests/integration/recovery
git commit -m "feat: support review recovery"
```

### 任务 19：实现真实安全扫描适配器

**文件：**
- 创建：`src/code_review_agent/adapters/security/scanner.py`
- 创建：`src/code_review_agent/adapters/security/logging.py`
- 创建：`src/code_review_agent/resources/security_policy.toml`
- 创建：`tests/integration/security/`

- [ ] 使用固定 detect-secrets detector，不启用网络验证、仓库 baseline、allowlist、过滤器或插件。
- [ ] 补充连接串、证书、PII、业务敏感标记和 unknown-sensitive 固定规则。
- [ ] 测试新增行、删除行、上下文、工具结果、模型回显、日志、错误和报告。
- [ ] 扫描 SQLite、报告和日志，确认模拟 secret 原值不存在。
- [ ] 测试扫描器崩溃、超时、冲突区间和无法解码时 fail closed。
- [ ] 提交：

```bash
git add src/code_review_agent/adapters/security src/code_review_agent/resources/security_policy.toml tests
git commit -m "feat: add secret scanning adapter"
```

### 任务 20：实现 OpenAI、Anthropic 和 compatible 模型适配器

**文件：**
- 创建：`src/code_review_agent/adapters/model/openai.py`
- 创建：`src/code_review_agent/adapters/model/anthropic.py`
- 修改：`src/code_review_agent/adapters/model/gateway.py`
- 创建：`tests/contract/model/`

- [ ] 使用 mock transport 验证最终请求摘要、token 计数/上界和输出硬限制。
- [ ] 明确关闭 retry、streaming、fallback 和环境代理继承。
- [ ] 测试连接、读取、总 deadline 和取消后的 known/unknown 映射。
- [ ] 测试 usage 归一化，不读取 reasoning/chain-of-thought 字段。
- [ ] 不运行产生真实费用的测试；真实调用放入手工验收并显式提供预算。
- [ ] 提交：

```bash
git add src/code_review_agent/adapters/model tests/contract/model
git commit -m "feat: add model provider adapters"
```

### 任务 21：实现凭证和 Provider Registry

**文件：**
- 创建：`src/code_review_agent/adapters/credentials/keyring_store.py`
- 创建：`src/code_review_agent/application/credential_service.py`
- 创建：`src/code_review_agent/adapters/registry.py`
- 创建：`tests/integration/test_credentials.py`
- 创建：`tests/contract/test_registries.py`

- [ ] 测试 set/get/clear/exists，secret 不进入 DTO、repr、日志和 SQLite。
- [ ] 测试无安全 keyring backend 时失败，不回退明文文件。
- [ ] 测试 Input/Model/Tool/Output Registry 声明、冲突、freeze 和精确版本解析。
- [ ] 测试恢复时不能自动切换 Provider、模型、origin、规则或工具版本。
- [ ] 提交：

```bash
git add src/code_review_agent/adapters/credentials src/code_review_agent/application src/code_review_agent/adapters/registry.py tests
git commit -m "feat: add credentials and registries"
```

### 任务 22：实现 GitHub PR 输入

**依据：** FR-002～FR-004，AC-06。

**文件：**
- 创建：`src/code_review_agent/adapters/input/github.py`
- 创建：`tests/contract/input/test_github_provider.py`
- 创建：`tests/acceptance/test_github_review.py`

- [ ] 测试只接受精确 `github.com` HTTPS PR URL，拒绝 userinfo/query/fragment 和其他域名。
- [ ] 测试公开/私有、Open/Draft、Closed/Merged、无凭证、失效和权限不足。
- [ ] 测试完整分页、base/head 固定、计数核对、超限和版本漂移。
- [ ] 测试上下文只按固定 SHA 和规范路径读取。
- [ ] 使用 respx/SDK fake 覆盖自动重试关闭、重定向和异常白名单映射。
- [ ] 提交：

```bash
git add src/code_review_agent/adapters/input/github.py tests
git commit -m "feat: support github pull requests"
```

### 任务 23：实现 GitLab MR 输入

**依据：** FR-002～FR-004，AC-07。

**文件：**
- 创建：`src/code_review_agent/adapters/input/gitlab.py`
- 创建：`tests/contract/input/test_gitlab_provider.py`
- 创建：`tests/acceptance/test_gitlab_review.py`

- [ ] 测试只接受精确 `gitlab.com` HTTPS MR URL。
- [ ] 测试公开/私有、Open/Draft、Closed/Merged、无凭证、失效和权限不足。
- [ ] 使用 `/diffs`，必要时 `/raw_diffs`；拒绝 collapsed、too_large、overflow、缺页和截断。
- [ ] 测试固定 SHA 上下文、版本漂移、超限和跨主机重定向。
- [ ] 提交：

```bash
git add src/code_review_agent/adapters/input/gitlab.py tests
git commit -m "feat: support gitlab merge requests"
```

### 任务 24：补齐 CLI 控制命令

**文件：**
- 修改：`src/code_review_agent/cli/commands.py`
- 修改：`src/code_review_agent/application/task_service.py`
- 修改：`src/code_review_agent/application/query_service.py`
- 创建：`tests/integration/cli/test_control_commands.py`

- [ ] 实现 `resume`、`credentials set|clear|status`、`providers list`、`terminate`、`cleanup`。
- [ ] 实现 report delivery 重试入口；若命令名未在 CLI 文档固定，先更新设计文档再实现。
- [ ] 测试 expected version、request ID、请求指纹和用户确认。
- [ ] 测试 terminate 不删除数据、不清零预算、不把 partial 标完整。
- [ ] 测试 cleanup 原子删除详细记录、保留墓碑和 Markdown。
- [ ] 提交：

```bash
git add src/code_review_agent/cli src/code_review_agent/application tests/integration/cli
git commit -m "feat: add task control commands"
```

### 任务 25：建立 14 组固定验收样例

**依据：** `docs/验收清单.md` AC-01～AC-14。

**文件：**
- 创建：`tests/fixtures/acceptance/AC-01/` 至 `AC-14/`
- 创建：`tests/fixtures/acceptance/manifest.toml`
- 创建：`tests/acceptance/test_acceptance_matrix.py`
- 创建：`evidence/test-runs/.gitkeep`

- [ ] 每个样例固定输入哈希、语言、预期发现、禁止发现、期望置信度和需求映射。
- [ ] 缺陷样例必须在运行前标注，不根据模型输出倒推预期。
- [ ] 干净样例必须人工复核，不把未知缺陷当误报。
- [ ] 平台样例使用隔离测试仓库和模拟凭证，不提交真实凭证。
- [ ] 验收 runner 输出机器可读结果和证据索引。
- [ ] 提交：

```bash
git add tests/fixtures/acceptance tests/acceptance evidence/test-runs
git commit -m "test: add fixed acceptance corpus"
```

### 任务 26：执行故障注入与安全验收

**文件：**
- 创建：`tests/acceptance/test_recovery_matrix.py`
- 创建：`tests/acceptance/test_security_matrix.py`
- 创建：`tests/acceptance/test_budget_matrix.py`
- 创建：`tests/acceptance/test_trace_matrix.py`
- 创建：`docs/测试与评估报告.md`

- [ ] 覆盖模型超时/限流、工具失败、平台临时失败、进程退出和机器重启等价模拟。
- [ ] 覆盖预算默认、非法、不足、耗尽、追加、unknown 和超额冻结。
- [ ] 覆盖提示注入、脚本诱导、上传密钥、提高预算和改变报告目标。
- [ ] 沿每条报告评论的 trace ID 核对输入、工具、模型、预算、验证和 checkpoint。
- [ ] 扫描数据库、报告、Trace、日志、错误和证据包中的模拟 secret。
- [ ] 填写 FR-001～FR-028 的证据映射，不包含 FR-010。
- [ ] 提交：

```bash
git add tests/acceptance docs/测试与评估报告.md evidence
git commit -m "test: verify recovery budget trace and security"
```

### 任务 27：整理 README 和交付材料

**文件：**
- 修改：`README.md`
- 创建：`config.example.toml`
- 创建：`docs/已知限制.md`
- 创建：`docs/AI协作案例.md`
- 修改：`docs/验收清单.md`

- [ ] README 写清范围、非目标、安装、配置、三种输入、预算、报告、恢复、凭证最小权限和安全边界。
- [ ] 记录固定模型、规则和工具版本。
- [ ] 提供至少一个可复现的完整演示。
- [ ] 精选 AI 协作记录，包含任务目标、上下文、方案、人工修正和验证结果。
- [ ] 明确不支持远程回评、企业自建平台、金额预算和仓库代码执行。
- [ ] 提交：

```bash
git add README.md config.example.toml docs
git commit -m "docs: prepare project delivery"
```

### 任务 28：干净环境最终验收

**文件：**
- 仅在发现问题时修改对应实现、测试或文档

- [ ] 在新的临时目录从仓库检出固定 commit。
- [ ] 运行：

```bash
uv sync --locked
uv run code-review-agent --help
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --cov=code_review_agent --cov-report=term-missing
```

- [ ] 运行至少 8 个固定样例，且覆盖 AC-01～AC-14 的全部主要预期。
- [ ] 三种输入均生成 Markdown 报告。
- [ ] 所有预置高置信度缺陷被发现；干净样例高置信度误报为 0。
- [ ] 正常、失败、partial、unknown、恢复、预算耗尽和安全拦截均有证据。
- [ ] 运行最终 secret 扫描，确认源代码、Git 历史、配置、夹具、报告、Trace、日志和文档无真实 secret。
- [ ] 更新 `docs/验收清单.md` 和 `docs/测试与评估报告.md`。
- [ ] 最终提交：

```bash
git add .
git commit -m "chore: complete final acceptance"
```

## 6. 阶段审查门禁

每完成一个任务，主 Agent 必须执行两次审查：

1. **规格审查：** 是否完整满足该任务引用的设计章节和 FR/AC。
2. **代码质量审查：** 是否存在 bug、安全风险、跨层依赖、不可恢复状态、隐式重试、secret 泄露或缺失测试。

发现问题时，把问题返回原实现 Agent 修复；修复后重新运行任务测试和审查。主 Agent 不应一边审查一边顺手扩展任务范围。

以下节点必须运行全量测试：

- 任务 7：SQLite UoW 完成；
- 任务 16：本地 diff 闭环完成；
- 任务 18：恢复完成；
- 任务 23：三种输入完成；
- 任务 26：验收矩阵完成；
- 任务 28：最终交付。

## 7. 推荐执行批次

为降低返工，按以下批次推进：

1. **批次 A，工程与信任基础：** 任务 0～7。
2. **批次 B，本地 diff 最小产品：** 任务 8～17。
3. **批次 C，恢复与真实适配器：** 任务 18～24。
4. **批次 D，验收与交付：** 任务 25～28。

每个批次结束后先演示当前可运行能力，再决定是否进入下一批次。不得为了尽快接真实模型而跳过安全、预算和 UoW 门禁。

## 8. 第一轮 Agent 指令

开始开发时，先给 Agent 以下任务，不要直接让它“实现整个项目”：

```text
执行《docs/superpowers/plans/2026-09-10-code-review-agent-development-plan.md》的任务 0。

只处理设计基线冻结，不写业务代码。阅读当前未提交的设计文档，核对 27 条 P0、
AC-01～AC-14、声明式工具裁决、schema/规则/模板版本。创建 docs/实现决策.md，
必要时只修改文档状态字段。完成后运行文档检查，报告发现的冲突、实际修改和验证结果。
不要自动进入任务 1，等待审查。
```

任务 0 审查通过后，再单独派发任务 1。后续始终保持“一次一个任务、审查后再继续”。

## 9. 完成定义

项目只有在以下条件全部满足时才算完成：

- FR-001～FR-009、FR-011～FR-028 全部通过；
- FR-025～FR-028 安全门禁全部通过；
- 三种输入均完成 Markdown 报告闭环；
- 所有预置高置信度缺陷被发现，干净样例高置信度误报为 0；
- checkpoint、Trace、预算、恢复和报告交付状态可相互核对；
- 数据库、日志、报告和交付材料中无真实或模拟 secret 明文泄露；
- 在干净环境中可按 README 从零复现；
- 不把 FR-010 远程回评表述为已交付能力；
- 不存在未解决 P0 缺陷。
