# ADR 0055：原生 Anthropic / Gemini 把历史中段 system 消息原位改写为带标签 user 文本

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/llm-provider-native.md § mid-history-system](../architecture/capabilities/llm-provider-native.md)；ADR 0004（cache-aware 压缩）/ 0020（预算提示）

## 背景

2026-09-28 能力侧 review 复现：Anthropic 与 Gemini 原生 provider 转换请求时对 `role="system"` 的消息直接
`continue`，注释称「已合并到顶层 system」，实际只合并了 `ApiRequest.system_prompt`。而内核在历史中段放的
system 消息恰恰是长程执行最关键的几样：压缩摘要、压缩后 pinned 重注、预算提示、长期记忆预取、业务注入。
后果：在这两家上一旦压缩，被压缩的历史就整段消失；认知回路原语在这两家上全部静默失效。

## 决策

1. **原位改写为 user 文本**，以 `<system-reminder>` 标签包裹。
2. **否决「并入顶层 system」**：每次注入都会改写 system，破坏 prompt cache 前缀（R2），且丢失注记在对话中的位置语义。
3. **否决「在 prompt 层统一改成 user」**：OpenAI 系原生支持中段 system，统一改写会无谓改变它们的 wire 形状与
   审计 attempt digest。改写只发生在不支持的 provider 内。
4. 相邻 user 合并：Anthropic 复用既有合并；Gemini 新增 user content 合并（避免连续同角色 content）。
   位于 `tool_result` 之后的注记追加在其后，`tool_result` 仍居首。

参照：claw-code `compact.rs` 以 user 角色承载压缩摘要。

## 验证

一致性测试覆盖三类 provider；端到端测试在 Anthropic（MockTransport）上跑 handoff 压缩，断言下一请求携带摘要。
修复前代码上 7/8 用例失败。真实端点未验证（环境无 Anthropic / Gemini key）。
