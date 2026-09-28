# ADR 0057：MCP 工具副作用分类默认保守，annotations 需显式信任

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/dynamic-tool-set.md § 副作用分类](../architecture/capabilities/dynamic-tool-set.md)；ADR 0025（effect 分类）/ 0045（工具崩溃对账）/ 0048（MCP 绑定）

## 背景

MCP 桥构造 ToolSpec 时不设 `effect_kind`，落到 ToolSpec 默认值 `pure`。ADR 0045 的冷恢复按它分流：
`pure` → 告诉模型「中断了，可安全重发」。于是任何 MCP 写操作（发消息、下单、改远端数据）在进程崩溃后
都会被引导重发，可能产生重复副作用。strict audit 也把它们记成无副作用。

## 决策

1. 桥接工具显式带分类，默认 `external_non_idempotent` / `manual`：崩溃后挂起交人，与 shell / http_request 等
   内置有副作用工具同级。
2. 新增 `trust_annotations`（默认 False）。打开后按 MCP `annotations` 细分：`readOnlyHint` → `pure` 且可并行；
   `idempotentHint` → `idempotent` / `retry`；其余保守。只认字面 `true`。
3. 重新同步时分类变化触发 `replace`。

## 否决的方案

- **默认信任 annotations**：MCP 规范明言它们是提示，不可信 server 的提示不得据以决策。误信一个错误的
  `readOnlyHint` 会让崩溃恢复重发写操作，方向上是不可逆损失；而保守默认的代价只是多一次人工裁决。
- **按 `destructiveHint` 再细分**：内核的恢复分流只区分「可重发 / 可回查 / 交人」，destructive 与否不改变分流，
  加一档没有消费方。

## 验证

`tests/mcp/test_mcp_annotations.py`：默认全保守、信任时六种 annotations 形态的分类、重同步替换、显式 parallel_safe 保留。
