# AI 协作案例

## 任务目标

在既有端口/适配器设计下，补齐凭证、Provider Registry、GitHub PR 和 GitLab MR
输入，并完成本地 review 闭环。

## 上下文组织

实现前先读取输入、基础设施和扩展适配器设计文档，确认所有输入必须归一到
`ChangeSet`，平台输入必须固定 base/head SHA，安全扫描和预算属于强制边界。
随后按依赖顺序实现公共身份模型、远程输入适配器、组合根接入和真实 Provider
验收。

## 方案和人工修正

真实 GitHub PR 验证暴露了两个边界问题：rename 文件不能使用普通同路径 diff
头；GitHub 文件接口的 `patch = null` 不能直接视为二进制。修正后增加了正确的
rename header 和同一 API 主机上的完整 diff fallback，同时继续拒绝跨主机重定向。

真实模型验证又发现模型偶尔返回 `findings: null` 或额外的 `summary` 字段。人工
检查领域协议后，在执行器系统规则中增加严格的 JSON 输出契约，要求唯一顶层字段
为数组形式的 `findings`，并补充单元测试。

## 验证

自动化测试覆盖凭证、Registry、三种输入、安全、预算、恢复、Trace 和固定验收
样例。真实 Provider 只使用显式配置和公开固定版本对象；真实凭证不进入代码、
报告、日志或证据包。
