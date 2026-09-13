# CR-Agent

CR-Agent 是一个本地、可恢复、可追溯的代码审查 CLI。它接收本地 unified
diff、GitHub.com Pull Request 或 GitLab.com Merge Request，输出 Markdown
报告，不向 PR/MR 发布评论。

## 范围与非目标

支持 Python 3.12、固定版本输入、金额预算、checkpoint、Trace、敏感信息扫描、
声明式只读工具和 Markdown 交付。首版不支持远程回评、GitHub Enterprise、
自建 GitLab、多用户身份、自动修复、自动合并，也不会执行被审查仓库的
脚本、构建、测试、typecheck、插件或依赖。

## 安装

```bash
uv sync --locked
uv run code-review-agent --help
```

## 配置

复制 `config.example.toml`，设置文件权限为 `0600`，再填写模型服务配置：

```bash
cp config.example.toml code-review-agent.toml
chmod 600 code-review-agent.toml
```

`code-review-agent.toml` is a local-only file and is ignored by Git. Never commit
provider credentials; keep `config.example.toml` limited to placeholders.

配置文件只用于本机可信用户。GitHub/GitLab 凭证应写入操作系统 keyring：

```bash
uv run code-review-agent credentials set github --secret '<token>'
uv run code-review-agent credentials set gitlab --secret '<token>'
uv run code-review-agent credentials status github
```

在 `[budget]` 中配置单次审查 CNY 上限和当前模型每百万 token 单价；系统向下取整
换算成整数 token 授权，且不得超过 `max_tokens` 安全上限。价格变化后应创建新任务，
不能把旧 checkpoint 按新价格继续使用。预算不足、模型调用结果不明或安全扫描无法
完成时，系统会停止或降级，不会伪装成完整成功。

## 三种输入

本地 diff：

```bash
git diff | uv run code-review-agent review --stdin \
  --provider openai-compatible --model qianfan-code-latest
```

文件输入：

```bash
uv run code-review-agent review --diff-file change.diff \
  --provider openai-compatible --model qianfan-code-latest
```

GitHub PR 或 GitLab MR：

```bash
uv run code-review-agent review \
  --url https://github.com/owner/repository/pull/123 \
  --provider openai-compatible --model qianfan-code-latest
```

报告默认写入 `reports/<task-id>.md`，状态和 Trace 可通过以下命令读取：

```bash
uv run code-review-agent status <task-id>
uv run code-review-agent trace show <task-id>
uv run code-review-agent providers
```

## 恢复和控制

暂停或结果不明的任务必须显式恢复；结果不明的模型调用还需要额外确认：

```bash
uv run code-review-agent resume <task-id> --confirm-unknown-retry
uv run code-review-agent terminate <task-id> \
  --reason "operator request" --expected-version 1 --confirm
uv run code-review-agent cleanup <task-id> --expected-version 1 --confirm
uv run code-review-agent report retry <task-id> --expected-version 1
```

`cleanup` 删除详细运行记录并保留墓碑和 Markdown 报告。它不会清零预算，也不会把
部分完成结果改写为完整结果。

## 安全边界

所有外部 diff、平台响应、工具结果和模型响应都按不可信文本处理。敏感内容在
进入模型、Trace、错误、日志和报告前统一扫描和脱敏；无法判断时按敏感处理。
仓库内容不能提高预算、改变输出目标、绕过规则或获得代码执行权限。

固定规则和工具版本见 `src/code_review_agent/resources/security_policy.toml`。
验收样例、矩阵和限制见 `tests/fixtures/acceptance/`、`docs/测试与评估报告.md`
和 `docs/已知限制.md`。
