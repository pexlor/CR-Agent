# OpenAI-Compatible Provider 设计

## 目标

为 Code Review Agent 增加一个真实的 OpenAI-compatible HTTP Provider，让一次
本地 `git diff` 审查可以经过安全扫描、标准化、规划、预算、模型调用、发现
收敛和 Markdown 报告交付的完整链路。

## 配置

Provider 配置全部来自显式 TOML 文件 `code-review-agent.toml`，不读取环境变量、
keyring 或其他隐式配置。配置结构如下：

```toml
[provider]
id = "openai-compatible"
version = "1"
model = "your-model"
origin = "https://api.example.com"
path = "/v1/chat/completions"
api_key = "sk-..."
timeout_seconds = 60
max_response_bytes = 1048576
max_output_tokens = 1024
structured_output = "json_object"
```

配置读取只允许明确支持的字段，Provider origin 必须是无路径、无 query、无
fragment、无 userinfo 的 HTTPS origin。请求路径必须是绝对 HTTPS 请求路径。
inline token 所在配置文件不能对 group 或 other 可读，权限不满足时拒绝启动。

## Provider 接口

新增 `OpenAICompatibleProvider` 实现现有 `ModelGatewayPort`。`prepare_request`
只构造不可变 `PreparedModelRequest`、计算 body digest 并记录 ownership，不发送
网络请求。`send_prepared` 使用 `httpx.AsyncClient` 发送一次请求，不自动重试、
不跟随跨 host 重定向、不使用流式响应、不追加 fallback。

请求固定使用：

- `POST`；
- `Authorization: Bearer <token>`；
- `Content-Type: application/json`；
- `stream: false`；
- `temperature: 0`；
- `response_format: {"type": "json_object"}`；
- 已配置模型和 `max_tokens`。

请求正文只包含上层已经生成的 system rules、审查 diff、工作单元、工具事实和
输出 schema，不在 Provider 内重新解析或修改审查范围。

## 响应和错误

支持 `choices[0].message.content` 为 JSON 字符串的 OpenAI-compatible 响应，并
读取合法的 `usage.prompt_tokens`、`usage.completion_tokens`。Provider 不验证
finding 业务字段，由执行层和发现处理层负责。

以下情况归一化为 `unknown`，禁止自动重试：

- DNS、连接、读取、写入或连接池超时；
- HTTP 408、429、5xx；
- 响应超出大小限制；
- 非 JSON、缺少 choices/content 或 content 不是字符串；
- usage 不完整或无法可信映射。

HTTP 400、401、403、404 等明确拒绝归一化为 `failed_known`。任何已发送请求
都不能伪装成未发送失败。

## 安全

Token 不出现在异常、日志、Trace、报告或稳定错误 details。请求不允许 query
传递凭据。Provider 不打印请求和响应原文，只返回稳定状态和脱敏摘要。HTTP
重定向默认关闭；响应体读取受 `max_response_bytes` 限制。

## 运行时装配

`bootstrap.build_runtime()` 根据 `provider.id` 选择 Provider。`local` 保留为
离线确定性 Provider，`openai-compatible` 使用新 HTTP Provider。CLI 参数
`--provider` 和 `--model` 继续校验配置值，应用编排和报告流程不变。

## 测试

使用 `respx` 覆盖：

- 请求 headers、JSON body、模型和结构化输出配置；
- 成功响应和 usage；
- 400、401、429、500；
- timeout、非法 JSON、非法 choices、超大响应；
- token 不出现在异常和日志；
- prepared request ownership、digest、单次发送和 discard；
- 配置文件权限、字段校验和无环境变量读取；
- 配置真实 Provider 后完成一次应用级审查并生成 Markdown。
