# ADR 0061：PostToolUse 可改写工具输出 + 工具结果统一字节上限

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/hooks.md § PostToolUse 改写](../architecture/capabilities/hooks.md)；[capabilities/turn-resource-guards.md § 工具结果字节上限](../architecture/capabilities/turn-resource-guards.md)；ADR 0036（offload）

## 背景

1. PreToolUse 能用 `args_override` 改参数，PostToolUse 的返回值却被丢弃。工具输出是 prompt injection 与敏感数据
   进入上下文的主通道（网页、MCP、检索结果），宿主没有任何位置在它进入历史前清洗或脱敏。
2. 工具结果没有统一上限。部分内置工具自带截断，MCP 与业务工具的输出可无限长地写进历史；一条 1MB 结果就能吃掉大半
   上下文窗口，之后只能靠压缩补救。`OffloadStrategy` 能无损落盘，但需要业务显式配置。

## 决策

1. PostToolUse handler 可返回 `output_override`（str）替换 `ToolResult.output`，多个 handler 链式生效；改写在事件上打
   `output_rewritten_by_hook`。仍不可否决。handler 抛异常 / 类型不对 → 记日志、输出不变，与既有审计型钩子同一约定；
   要 fail-closed 的清洗钩子自行捕获异常并返回安全文本。
2. `ContextBudget.max_tool_result_bytes`（默认 128KiB）在 PostToolUse 之后、回填历史之前按字节截断，保头 60% / 尾 40%，
   中间写明上限与省略字节数，事件附 `output_capped`。
3. compressors 含 `OffloadStrategy` 时不截断，大结果交给 offload 无损处理。
4. 实现集中在 `loop/tool_output.py`；`dispatch_batch` 新增 `result_cap_bytes` 参数，三个派发点都传。

## 否决的方案

- **钩子异常时 fail-closed（把结果替换为错误）**：会改变所有既有 PostToolUse 钩子的失败语义（它们目前是审计用途），
  且宿主自行 fail-closed 只需一个 try/except。
- **上限挂在每个 ToolSpec**：上限本质是「一条结果最多占多少上下文」，属于上下文预算；逐工具配置会漏掉 MCP 与动态注册工具。
- **默认不开上限**：不开就等于维持现状的无界输入。128KiB 远大于内置工具自身的截断阈值，对既有工具无影响，只拦异常大的输出。
- **截断在 prompt 视图层做（历史存全文）**：token 估算、压缩摘要、审计都读历史，视图层截断会让它们与模型实际看到的不一致。

## 影响

- R1 无业务概念；R2 截断发生在结果首次入历史前，确定性，不影响已缓存前缀；R3 两个事件字段；R4 / R5 无变化。
- 行为变化：超过 128KiB 的工具结果被截断（此前原样入历史）。需要全文的业务设 `max_tool_result_bytes=None` 或配置 offload。

## 验证

`tests/loop/test_tool_output.py`（12 例）：未超限不变、ASCII / CJK / emoji 截断长度与 UTF-8 合法性、错误标记保留、
过小上限拒绝、offload 存在时不截断、钩子链式改写、异常与非 str override 被忽略且后续 handler 照常执行、
真实 pool 中清洗后的输出进入下一次请求、超大结果截断后才入历史与 JSONL。
