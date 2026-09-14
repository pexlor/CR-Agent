# 配置化金额预算设计

## 目标

单次审查的金额预算和当前模型单价完全来自本地配置，不要求 CLI 或调用方注入
token 预算。预算执行仍使用现有整数 token 账本。

## 配置与换算

`[budget]` 配置 `max_cost_per_review_cny` 和
`price_per_million_tokens_cny`。两者使用十进制字符串，避免二进制浮点误差。
启动时按以下公式向下取整：

```text
authorized_tokens = floor(max_cost_per_review_cny * 1_000_000
                          / price_per_million_tokens_cny)
```

金额和单价必须为有限正数；换算结果必须至少为 1 token，且不得超过
`max_tokens` 安全上限。无效配置直接拒绝，不静默回退或截断。

## 数据流与报告

CLI 不再暴露 `--budget-tokens`。`ConfiguredRuntime` 从固定 `CliConfig` 获取换算后的
授权 token，并把该值固化进任务与 checkpoint 绑定。恢复继续使用任务启动时保存的
token 授权；价格配置变化会改变 checkpoint 配置摘要，不能把不同价格条件下的结果
混用。

预算账本继续记录 token，以保持整数预留和保守结算。Markdown 报告额外显示 CNY
金额上限、每百万 token 单价、授权成本、已知成本、不确定成本与剩余额度。金额展示
由 `Decimal` 计算，不使用 float 累计。
