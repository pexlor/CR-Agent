# CR-Agent

Code Review Agent 项目：支持输入 GitHub PR、GitLab MR 或 `git diff`，并生成可追溯的代码审查结果。

当前仓库包含需求拆解、验收清单、开发流程及相似项目实现参考等前期文档。

## 使用真实 Provider

复制 `code-review-agent.toml.example` 为 `code-review-agent.toml`，填写
OpenAI-compatible 服务地址、模型和 token，并将配置文件权限设为 `0600`：

```bash
chmod 600 code-review-agent.toml
git diff | uv run python -m code_review_agent review \
  --stdin \
  --provider openai-compatible \
  --model your-model
```

配置只从 TOML 文件读取，不使用环境变量。Provider 不自动重试；连接超时、
429、5xx 或响应不确定时会产生 `unknown` 结果，不会伪装成完整成功。
