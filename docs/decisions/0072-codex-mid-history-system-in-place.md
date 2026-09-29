# ADR 0072：codex 协议把历史中段 system 注记原位改写为带标签 user 消息

- 状态：Accepted
- 日期：2026-09-29
- 关联：ADR 0026（独立 codex provider）/ 0055（Anthropic / Gemini 同类修复）/ 0065（pinned 周期重注）；[capabilities/llm-codex-provider.md § 3.1](../architecture/capabilities/llm-codex-provider.md)
- Amends #0026

## 背景

ADR 0026 发现 codex 代理拒收 `role=system` 的 input item，于是把历史中段的运行时 system 注记（预算提示、记忆预取、压缩摘要、
pinned 重注）按顺序折叠进顶层 `instructions`。台账新场景 `pinned_periodic` 暴露了两处代价：

1. **位置丢失**：周期重注在内核请求里位于尾部，到 wire 上却排到对话之前；模型把对话里更早的 `todo_write` 输出当成「最近看到的」，
   同一场景 5 次真实运行挂了 3 次。
2. **缓存前缀被改写**：每次注入都改变 `instructions`，prompt cache 前缀随之失效，与 ADR 0065「尾部追加、R2 安全」在 wire 层不一致。

## 决策

中段 system item 原位改写为 `role=user` 的 message（单个 `input_text`，`<system-reminder>` 包裹，复用 `mid_history_system_text`）；
顶层 `instructions` 只含 `ApiRequest.system_prompt`。与 ADR 0055 对 Anthropic / Gemini 的处置一致，codex 加入同一跨 provider 一致性测试。

## 否决的方案

- **保持折叠进 instructions**：见背景两处代价。
- **改写为 `role=developer` item**：代理对 developer item 的接受度未经验证，且官方 Responses 对 developer 的语义与 system 相同，
  很可能同样被拒；user + 标签已在 Anthropic / Gemini 上验证可行。

## 影响

- R2：注入不再改写 `instructions`，已缓存前缀保持稳定。R1 / R3–R5 无变化。
- 审计请求摘要基于 provider-neutral 的 `ApiRequest` 计算，不受 wire 形状变化影响。
- 行为变化：codex 上中段注记从「系统指令」变为「带标签的用户侧注记」，模型对其权威性的感知可能略有变化；由真实台账全量回归覆盖。

## 验证

`tests/llm/test_codex_wire.py`：中段注记原位成为带标签 user 消息、instructions 不随注入变化；`tests/llm/test_mid_history_system.py`
跨 provider 一致性新增 codex。真实台账全量重跑（含 `pinned_periodic` / `compaction_continuity` / `budget_awareness`）。
