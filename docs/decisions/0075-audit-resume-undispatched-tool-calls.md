# ADR 0075：审计 resume 收敛「模型回复已落账、意图尚未登记」的工具调用

- 状态：Accepted（Amends #0070）
- 日期：2026-09-30
- 关联：[tool-crash-reconciliation § 从未登记意图的调用](../architecture/capabilities/tool-crash-reconciliation.md)；
  [session-journal-business-integration §13.2](../architecture/capabilities/session-journal-business-integration.md)；
  ADR 0025 / 0045 / 0053 / 0070

## 背景

strict audit 下一次带工具调用的采样产生两个相继的 batch：

```text
batch A：llm_response_committed + conversation_item(function_call ...)
batch B：tool_intent_committed × N（整批意图，先于任何派发）
```

ADR 0070 让 resume 能收敛「有意图、结局未知」的调用。进程死在 A 与 B 之间时是另一种形态：Journal 里没有意图，
`find_unsettled_effects` 的配对扫描看不到任何未结算项，resume 照常通过，root history 末尾却留着没有结果的
`function_call`。续跑的第一次请求就带着不成对的调用发给 provider。

在 main（c3aa46b）用「core 在意图 batch 落账前关闭」复现：resume 成功，history 中该调用没有任何
`function_call_output`。

## 决策

1. **结局是确定的，不走 ADR 0070 的分流**。整批意图先于任何派发原子落账，没有意图即没有执行。不回查、不看
   `effect_kind`、不征求人裁决——这三者都是为「可能已执行」准备的。
2. **结论写成新记录 `tool_call_undispatched`**（`ToolCallUndispatchedV1`），不复用 `tool_recovery_committed`：
   后者的必填字段 `intent_record_id` / `effect_kind` 在这里不存在，为兼容而放宽会削弱既有记录的校验。
3. **挂在该调用本应使用的 tool operation 下**（`{turn}:tool:{call_id}`，由 `function_call` 会话项的
   thread / submission / turn 派生）。审计时按 tool operation 查一个调用，能同时看到「模型发出了它」与
   「它没有被派发」。补写的 `function_call_output` 会话项沿用 ADR 0070 的 ordinal 1。
4. **按 Journal seq 顺序配对识别**：`function_call` 之后出现的同 call id 的意图、结果会话项或既有结论都结算它。
   不按「call id 是否出现过」判断——同一 thread 内 call id 可能被复用。
5. **与 ADR 0070 的结论同一个 batch、同一条全有或全无规则**：任一调用仍需人处置，本次一条不写。
6. **无法构成 operation identity 的 call id 交人**：call id 为空或含 `:` 时 live 路径同样登记不了意图。只读预检
   即拒并列出该 `function_call` record，不写接管记录。
7. **回填文本以 `not_executed:` 开头、`is_error=true`**，明确告诉模型可以安全重发；处置结论取新值
   `not_dispatched`，与「副作用声明可重发」的 `safe_to_retry` 区分——两者依据不同，可观测上不应混为一谈。

## 替代方案

- **把 A、B 合并成一个 batch**：从源头消除窗口。但意图依赖派发层对参数的裁决（`effective_arguments`、
  `invalid_arguments` 拒绝、工具是否已提供），这些在模型回复提交时尚未计算；合并等于把派发前裁决挪进 LLM 提交
  路径，改动面远大于恢复侧补一条记录。且已有 Journal 里的这种形态仍需恢复路径处理；否决。
- **resume 时丢弃末尾不成对的 `function_call`**：改写了「模型发出过这些调用」的事实，违反 append-only；否决。
- **prompt 组装时临时合成结果**：结论不 durable，每次组装都要重新推断，审计链上也查不到依据；否决。
- **复用 `tool_recovery_committed` 并新增 basis**：见决策 2；否决。

## 后果

- 新 record type `tool_call_undispatched`；`Disposition` 新增 `not_dispatched`；
  `conversation.journal` 导出 `ToolCallUndispatchedV1` / `TOOL_CALL_UNDISPATCHED_RECORD_TYPE`。
- 此前这种形态的 Journal 能 resume 但续跑会失败；现在 resume 时多追加一个恢复 batch。没有这种形态的 Journal
  行为不变。
- 子 thread 上的同类调用不在本决策范围：子 thread 崩溃必然伴随父 thread 未结算的 skill 派发，整体仍 fail closed。
- R1–R5：R1 无业务概念；R2 补写的结果追加在 history 末尾，不动已缓存前缀；R3 结论随 `thread_resumed` 透出并
  durable 在 Journal；R4 纯计算 + 一次有界追加；R5 本决策即 R5 在该崩溃窗口上的补全。

## 验证

`tests/loop/test_audit_resume_undispatched.py`（引擎级：core 在意图 batch 落账前关闭模拟进程死亡，另一 core 实例
resume）覆盖单个调用收敛并续跑、并行批次全部收敛且顺序一致、结论 durable 且 strict verify 通过、二次崩溃不重复
收敛、call id 无法构成 identity 时预检即拒且 Journal 无新增、正常跑完的调用不被误判。
