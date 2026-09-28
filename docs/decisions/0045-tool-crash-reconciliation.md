# ADR 0045：工具执行途中崩溃的冷恢复——写前意图 + 按副作用类型分流

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/tool-crash-reconciliation.md](../architecture/capabilities/tool-crash-reconciliation.md)；ADR 0025（strict audit 恢复语义）；[suspend-resume](../architecture/capabilities/suspend-resume.md)

## 背景

2026-09-28 内核 review 核实：非审计的 Chat 路径把 `function_call` 放在工具执行**之后**与 output
成对写（`turn_sample.py` 阶段 3，为保交错 transcript 结构与 rewind 切点）。执行期间崩溃 = 调用
意图从未落盘，resume 后模型看不到这次调用，大概率重发——非幂等副作用静默重复。Responses 路径
虽然先落了 `function_call`，恢复时却一刀切回填「未知，不重试」。`ToolSpec.effect_kind` /
`reconciliation` 早已声明（ADR 0025），恢复路径却不读。

## 决策

1. **写前意图，不改 transcript 结构**：派发前落一条记账类 `tool_intent`（不进 LLM 视图），而不是把
   `function_call` 挪到执行前——后者会把交错的 `fc, fco, fc, fco` 改成 `fc, fc, fco, fco`，破坏
   rewind `retry_tool` 的切点语义和既有字节级一致性。
2. **意图同时进 hot history**：冷重建按 hot 下标应用 rewind `cut_index`；只写 store 会让下标漂移。
3. **按副作用分流**：pure / idempotent → 告知可安全重发；有 `reconcile` → 回查真实结局；其余 → 交人。
   恢复**从不自动执行工具**——即便是幂等工具，也由模型决定是否重调（恢复发生在 turn 之外，没有
   完整的 ToolContext 语境）。
4. **交人复用挂起 / Resume**：新增 `SuspendReason.TOOL_OUTCOME_UNKNOWN`，裁决 `retry`（复用 permission
   allow 的执行路径）/ `provide`（原样回填，保留 is_error）/ `abort`。活跃挂起守卫天然阻止「越过未知
   结局继续跑」。不另起一套冻结机制（那是 strict audit 的语义）。
5. **保留旧行为开关**：`EnginePool(tool_recovery="report")` 退回「结果未知，不重试」，给没接 Resume
   UI 的宿主一个不会卡住会话的选项。默认 `suspend`——正确性优先。

## 后果

- 未声明 `effect_kind` 的业务工具默认 `pure`，崩溃后会被告知「可安全重发」。有副作用的业务工具**应当**
  显式声明 `effect_kind`（内置有副作用工具均已声明为 `external_non_idempotent`）。
- 默认模式下，崩溃遗留非幂等调用的会话在人裁决前不接受新用户消息——这是有意的。
- 记账类 item 的 token 估算从 50 改为 0（顺带修正高估）。
