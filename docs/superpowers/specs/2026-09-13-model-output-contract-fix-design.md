# 模型输出契约修复设计

## 背景

真实 `qianfan-code-latest` 调用能够识别代码缺陷，并返回合法 JSON；但当前
OpenAI-compatible Provider 只发送 `response_format = json_object`，没有把
`PromptEnvelope.output_schema` 放入请求。执行器随后要求每条 finding 同时包含
八个固定字段，导致模型返回的合理但不同形状的数据被整体判为
`model_output_invalid`。汇总层又把该原因覆盖成 `coverage_degraded`，最终报告只显示
“未审查”，丢失了可诊断原因。

## 目标

1. 让所有 OpenAI-compatible 模型收到完整、确定的 finding JSON Schema。
2. 保持响应解析严格，不猜测或自动映射未声明字段。
3. 非法模型输出必须在覆盖范围和 Markdown 报告中保留
   `model_output_invalid` 原因。
4. 使用真实千帆模型复验非空 finding 能进入最终报告。

## 非目标

- 不在本次修复中实现完整 Trace 持久化。
- 不引入特定于千帆的响应字段转换。
- 不切换到兼容性尚未验证的原生 `response_format = json_schema`。
- 不改变预算、重试、敏感信息扫描或工具授权语义。

## 方案

### 请求契约

Provider 保留 `response_format = {"type": "json_object"}`，并将
`PromptEnvelope.output_schema` 通过稳定的 canonical JSON 序列化后附加到 system
message。提示必须说明响应只能是符合该 schema 的单个 JSON 对象。

这使契约由拥有 Provider 请求组装职责的适配器统一发送，同时继续兼容只支持
JSON object mode 的服务。

### 响应处理

执行器继续把模型响应视为不可信输入，并严格要求：

- 顶层只有可解析的 `findings` 数组；
- 每个 finding 包含 `category`、`title`、`problem`、`trigger_condition`、
  `impact`、`suggestion`、`change_causation` 和 `limitations`；
- 任一条目不满足契约时，不交付部分猜测结果。

本次不增加宽松转换层，避免把语义不完整的数据包装成高置信度评论。

### 错误传播

模型调用成功但响应契约无效时，工作单元仍记录模型调用已成功和已知 token 用量，
但覆盖状态必须明确为未审查，原因保持为 `model_output_invalid`。FindingProcessor
在处理 degraded coverage 时优先传播工作单元的具体 `error_code`，仅在没有错误码时
回退为 `coverage_degraded`。

Markdown 报告因此能够区分模型输出无效、工具覆盖降级和其他未审查原因。

## 测试设计

按 TDD 顺序增加以下回归测试：

1. Provider 请求测试：断言请求 system message 包含 canonical JSON Schema，且仍使用
   `json_object` response format。
2. 执行与汇总测试：构造缺少八字段契约的真实风格响应，断言最终 coverage 原因为
   `model_output_invalid`。
3. 真实形状集成测试：返回一个完整非空 finding，断言运行结果为
   `complete_with_findings`，Markdown 包含标题、问题、影响和建议。
4. 全量回归：运行 Ruff、mypy、pytest 和 acceptance suite。
5. 真实 Provider 复验：使用固定 Git diff 调用 `qianfan-code-latest`，确认报告不再是
   `partial/coverage_degraded`，并检查输出目录没有模型密钥明文。

## 安全与兼容性

- Schema 不包含凭证或用户配置，可以进入出站请求和测试断言。
- API key 继续只存在于本机权限为 `0600` 的配置中，不写入测试、报告或提交。
- 对不遵循 schema 的模型保持 fail-closed，结果为部分完成而非伪造完整成功。
- 不改变现有 HTTP 状态、超时、限流和不确定用量处理逻辑。

## 验收标准

- 新增测试在生产代码修改前能稳定复现失败。
- 新增测试和现有 440 项测试全部通过。
- Ruff 和 mypy 通过。
- 真实千帆调用返回的有效发现进入 Markdown Findings。
- 如果真实模型仍返回非法结构，报告明确显示 `model_output_invalid`。
- 报告、状态库和 evidence 目录中不存在配置密钥明文。
