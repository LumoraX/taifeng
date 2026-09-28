# ADR 0043：上下文 token 计数以 provider 实测校准 + usage 口径归一

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/token-accounting-calibration.md](../architecture/capabilities/token-accounting-calibration.md)；[context-compression 活文档](../architecture/context-compression.md)；ADR 0004（cache-aware 压缩）/ 0020（预算自知）/ 0036（cache anchor 真相）

## 背景

2026-09-28 内核全面 review：压缩触发、预算提示、发送前预检只用 `len(text)/3.5` 粗估
（`context/budget.py`），provider 回报的真实 usage 只进 K2 记账和 cache 统计，从不参与上下文判断。
粗估看不到 system prompt / 工具 schema / 协议模板，CJK 文本还严重低估——结果是压缩时机靠运气，
真撞上限时靠 overflow 自愈兜底（多一次失败请求）。`ContextBudget` 也不区分输入与输出预留。

同时发现 Anthropic usage 映射把上游 `input_tokens`（**只含未命中缓存**的部分）原样当作
`TokenUsage.input_tokens`，而其余 provider 的该字段**含**缓存部分：Anthropic 上 K2 会话累计、
`cached_ratio`（甚至可 > 1）系统性失真。

按 ADR 0017 属规则①（内核机制缺口）：上下文占用是内核自己的内存管理判据。

## 决策

1. **口径归一**：`TokenUsage.input_tokens` 统一为「完整 prompt token 数」。Anthropic 在
   `extract_usage_anthropic` 内归一为 `uncached + cache_creation + cache_read`。这是对外可见的
   数值变化（Anthropic 用户的 `usage.input_tokens` / `total_tokens` 会变大），但旧值是错的。
2. **实测锚点 + 增量粗估**（codex 范式）：采样成功时记 `TokenCalibration`（发出时 history 长度、
   末项 id、实测 prompt、overhead）；估算 = 实测 + 锚点后新增条目粗估。锚点与 cache anchor 同一时刻推进。
3. **失效而非丢弃**：前缀被压缩改写 / rewind 截断时锚点失效，但 overhead 保留继续修正粗估——
   比退回纯粗估更准，且无需重新采样。
4. **不做负修正**：overhead 下限 0。粗估偏高时宁可早压缩，也不冒低估撞上限的风险。
5. **输出预留**：`ContextBudget.output_reserve_tokens`（默认 0，零行为变化），阈值按「窗口 − 预留」计算。
   取值是策略（R1），由业务按 `max_output_tokens` 注入。

## 备选与否决

- **引入 tokenizer 协议（tiktoken 等）**：否。要为每家模型维护 tokenizer，仍看不到 provider 侧模板开销；
  实测 usage 免费且权威。粗估只承担「两次采样之间的增量」，误差被限制在尾部。
- **按比例校准（实测/粗估 的比值乘全量）**：否。overhead 是近似常量（system + tools），不随 history
  线性变化；比例法在长会话里会把 overhead 误差放大。
- **锚点持久化**：否。重建成本只是一次采样，持久化要处理跨进程 provider 变更等失效问题。

## 顺带修复

`handle_update_budget` 此前逐字段重建 `ContextBudget`，会把 `max_request_bytes` 悄悄重置为 None。
改为 `dataclasses.replace` 只覆盖显式字段；非法组合记 error 并保持原预算。
