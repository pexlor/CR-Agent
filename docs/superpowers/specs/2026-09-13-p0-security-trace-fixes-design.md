# P0 安全与 Trace 修复设计

## 目标

关闭当前验收发现的三个 P0 问题：仓库跟踪本地凭证配置、checkpoint
反序列化动态导入仓库模块、评论 Trace ID 无法查询。

## 设计

本地运行配置只由 `config.example.toml` 生成，`code-review-agent.toml` 不再纳入
版本控制，并由 `.gitignore` 阻止误提交。现有疑似真实凭证从工作树移除；凭证本身
仍需在提供方轮换。CLI 模块导入不读取本地配置，配置加载延迟到 `main()` 真正执行，
使源码检验、测试收集和库调用不依赖未跟踪文件。

checkpoint 保留现有 JSON 格式，但类型恢复从“按字符串动态 import”改为应用内的
精确类型白名单。白名单键仍使用原类型标识，以兼容已有可信 checkpoint；未知类型、
`tests.*` 和 `test_*` 一律按损坏 checkpoint 拒绝，不触发模块加载。

评论 Trace 使用现有 `FinalFinding.trace_id` 作为外部查询键。持久化层新增
`review_finding_traces` 映射，记录 trace、task、finding 和 work unit。查询先按 task ID
读取完整时间线；若输入是评论 Trace ID，则只返回任务级上下文、对应 work unit 的
工具/模型/预算事件、该 finding 的校验事件和报告事件。映射与 Trace 使用相同的七天
保留期，并随任务清理。

## 验证

- 恶意 checkpoint 类型在任何 import 发生前被拒绝。
- 报告中的评论 Trace ID 可直接查询，并包含对应模型请求、回复和 finding 事件。
- 本地配置文件不再被 Git 跟踪或允许误提交。
- 定向测试、全量测试、Ruff、mypy 和 `git diff --check` 全部通过。
