# 模型输出契约修复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 按任务实现此计划。步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 让 OpenAI-compatible Provider 明确发送完整 finding schema，并在模型输出无效时把 `model_output_invalid` 保留到最终报告。

**架构：** Provider 继续使用兼容性更广的 JSON object mode，同时把 canonical JSON Schema 附加到 system message。执行器保持严格解析；FindingProcessor 在 coverage degraded 时传播执行器的具体错误码。集成测试覆盖非空 finding 从 HTTP 响应到 Markdown 报告的完整路径。

**技术栈：** Python 3.12、httpx、pytest、respx、Ruff、mypy。

---

## 文件结构

- 修改 `src/code_review_agent/adapters/model/openai_compatible.py`：发送完整输出 schema。
- 修改 `src/code_review_agent/domain/findings/processor.py`：保留 degraded execution 的具体错误码。
- 修改 `tests/contract/test_openai_compatible_provider.py`：验证实际请求包含 canonical schema。
- 修改 `tests/unit/domain/findings/test_processor.py`：验证 `model_output_invalid` 不被覆盖。
- 修改 `tests/integration/model/test_openai_provider_review.py`：验证非空 finding 进入 Markdown 报告。

### 任务 1：发送完整输出 Schema

- [ ] **步骤 1：编写失败的 Provider 请求测试**

在 `test_prepare_builds_fixed_request_without_exposing_token` 中解析 system message，断言其中包含 `canonical_json(dict(_envelope().output_schema))`，同时继续断言 response format 为 `json_object`。

- [ ] **步骤 2：运行红灯测试**

运行：

```bash
uv run --python 3.12 pytest -q tests/contract/test_openai_compatible_provider.py::test_prepare_builds_fixed_request_without_exposing_token
```

预期：因当前 system message 不包含 schema 而失败。

- [ ] **步骤 3：实现最小请求修复**

在 `prepare_request` 中将 schema 稳定序列化，并构造：

```python
schema = canonical_json(dict(envelope.output_schema))
system_rules = (
    f"{envelope.system_rules}\n"
    "Output JSON Schema (follow exactly):\n"
    f"{schema}"
)
```

请求其余字段保持不变。

- [ ] **步骤 4：运行绿灯测试**

运行同一步骤 2，预期通过。

### 任务 2：保留模型输出错误原因

- [ ] **步骤 1：编写失败的 FindingProcessor 测试**

构造 `state="succeeded"`、`coverage_impact="degraded"`、
`error_code="model_output_invalid"` 的 execution，断言 coverage entry 的 reason code
为 `model_output_invalid`。

- [ ] **步骤 2：运行红灯测试**

运行：

```bash
uv run --python 3.12 pytest -q tests/unit/domain/findings/test_processor.py::test_coverage_preserves_degraded_execution_error_code
```

预期：实际 reason code 为 `coverage_degraded`，测试失败。

- [ ] **步骤 3：实现最小错误传播修复**

在 degraded coverage 分支使用：

```python
state = CoverageState.UNREVIEWED
reason = getattr(execution, "error_code", None) or "coverage_degraded"
```

- [ ] **步骤 4：运行绿灯测试**

运行同一步骤 2，预期通过。

### 任务 3：覆盖非空 Finding 端到端交付

- [ ] **步骤 1：扩展集成测试为完整非空响应**

在 `tests/integration/model/test_openai_provider_review.py` 新增测试，mock HTTP
响应中的 `findings` 条目包含八个必需字段。断言结果为
`complete_with_findings`，报告包含 finding 标题、问题、影响、建议和 trace ID。

- [ ] **步骤 2：运行集成测试**

运行：

```bash
uv run --python 3.12 pytest -q tests/integration/model/test_openai_provider_review.py
```

预期：通过；若失败，只修复本设计范围内的数据契约问题。

- [ ] **步骤 3：提交实现**

只提交上述源文件、测试和本计划，不提交 `code-review-agent.toml` 或运行产物。

### 任务 4：全面与真实复验

- [ ] **步骤 1：运行静态与自动化检查**

```bash
uv run --python 3.12 ruff check src tests
uv run --python 3.12 mypy src
uv run --python 3.12 pytest -q
uv run --python 3.12 pytest -q tests/acceptance
git diff --check
```

- [ ] **步骤 2：运行真实千帆模型审查**

在主工作区使用权限为 `0600` 的本地配置，对固定真实 Git diff 执行 CLI review。
预期状态为 `complete_with_findings`，Markdown 包含模型识别到的返回类型问题，且报告、状态库和证据目录不包含密钥明文。

- [ ] **步骤 3：核对工作区**

确认实现提交不包含配置密钥，主工作区只保留用户已授权的本地配置变更和本次修复。
