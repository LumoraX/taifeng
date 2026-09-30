# ADR 0076：审计 resume 沿 skill 派发树自底向上收敛子 thread 的工具调用

- 状态：Accepted（Amends #0070）
- 日期：2026-09-30
- 关联：[tool-crash-reconciliation § 沿 skill 派发树收敛](../architecture/capabilities/tool-crash-reconciliation.md)；
  [session-journal-business-integration §13.3](../architecture/capabilities/session-journal-business-integration.md)；
  ADR 0025 / 0045 / 0053 / 0070 / 0075

## 背景

ADR 0070 的工具收敛只作用于 root thread，理由是「子 thread 的调用必然伴随未结算的 skill 派发，整体仍 fail
closed」。后果：只要崩溃发生在同步 `call_skill` 的子 skill 执行途中，这个 Session 就只能废弃——即便子 skill 里那次
调用可回查或本就幂等。子 skill 承担具体工作是常态用法，这个限制让审计 resume 在多数真实崩溃下不可用。

「未结算的 skill 派发」不是一个独立的未知量。派发本身没有外部副作用，它的全部副作用都发生在子 thread 的工具调用
里，而这些调用各自有 durable 意图，既有规则已经能逐个判定。

## 决策

1. **把派发这一层的未结算拆解到其内部的工具调用**，不为派发引入新的人裁决入口。可收敛 thread 从 root 扩展为
   「root + 被中断派发的子 thread」，父 thread 可收敛时子 thread 才可收敛，逐层传递。
2. **自底向上、同一个 batch**：子 thread 的调用（ADR 0070 / 0075 的规则原样适用）→ 派发终态 → 父 `call_skill`
   调用。记录按因果顺序排列；全有或全无覆盖整棵树。
3. **悬空 `call_skill` 意图按派发谱系结算，不看 `effect_kind`**。`call_skill` 声明为
   `external_non_idempotent / manual` 是因为它的副作用取决于子 skill；恢复时子 skill 内每个调用的结局都已查清，
   再按外层声明交人是重复裁决。四种谱系形态：
   - 没有 `skill_selected` → 派发从未开始（`skill_selected` 先于一切派发动作落账）；
   - 有 selected、无 started → 子 thread 从未创建；补 `skill_dispatch_finished(rejected)`，沿用「rejected 不带子
     谱系」的既有校验；
   - 有 finished → 子 skill 已有 durable 终态，用它还原父调用本应得到的结果；
   - 有 started、无 finished → 被中断。
4. **被中断的派发补写与 live 路径同类型、同 record id 的终态记录**（`skill_dispatch_finished` +
   `thread_terminal`），actor 为 `system/recovery`。不新增 record type：派发确实终止了，既有类型能完整表达，
   Timeline / 扫描等消费方无需认识新类型。状态取 `cancelled`、`end_reason="process_recovery"`。
5. **不记战绩**：不写 `skill_outcome`。进程崩溃不是 skill 的成败，计入会污染按战绩算分的数据（工具认知回路相位 5）。
6. **父调用的结果列出直接子层各调用的处置**。模型需要知道「子 skill 里那次写入其实已经完成」才能决定要不要
   重新派发；只告诉它「被中断」会诱发重复副作用。更深层的结论不上卷——直接子层里的 `call_skill` 处置
   （`dispatch_interrupted`）已经提示那一层不完整。
7. **`basis` 新增 `dispatch`，`verdict` 新增 `not_started` / `interrupted`**：复用 `tool_recovery_committed`
   而不另立记录类型，因为被结算的对象确实是一条 `tool_intent_committed`，既有字段全部适用。
8. **恢复不续跑子 skill**。同步派发的父 turn 已随进程消失，没有可以接回结果的调用栈；续跑属于「执行」，
   审计模式的 effect 只能发生在有 durable intent 的 turn 内（ADR 0070 决策 7）。
9. **子 thread 投影一并核对**：resume 对恢复写过记录的子 thread 调用 `reconcile_resumed_thread`，缺后缀补齐、
   分叉只标 stale。

## 替代方案

- **为被中断的派发新增人裁决入口**（让人决定整个子 skill 算成功还是失败）：人无从判断一段没跑完的执行；
  真正需要人判断的只有其中结局不明的那几个调用，它们已有入口；否决。
- **resume 后续跑子 skill**：见决策 8；否决。
- **新增 `skill_recovery_committed` 记录类型**：见决策 4；否决。
- **被中断的派发记一条 `abandoned` 战绩**：见决策 5；否决。
- **父调用结果上卷整棵子树的处置**：深层调用的处置对 root 的模型缺少上下文（它不知道那些工具是什么），
  徒增 token；否决。

## 后果

- 同步 `call_skill` 子 skill 执行途中崩溃的审计 Session 可以 resume。
- `RecoveryBasis` / `RecoveryVerdict` / `Disposition` 的取值集合扩大（新增 `dispatch`、`not_started` /
  `interrupted`、`dispatch_interrupted`）；旧 Journal 冷读行为不变。
- 悬空 `call_skill` 意图此前一律交人，现在自动结算——业务若依赖「`call_skill` 崩溃必经人裁决」需改为在
  resolver 之外自行检查 `thread_resumed.recovered_tool_calls`。
- 仍不覆盖：未结算的 LLM attempt（请求已落账、无 checkpoint）出现在任一 thread 上都 fail closed；detached spawn
  在审计模式下本就被 capability gate 拒绝。
- R1–R5：R1 无业务概念；R2 补写的结果追加在各 thread history 末尾，不动已缓存前缀；R3 root 调用的处置随
  `thread_resumed` 透出，全部结论 durable 在 Journal；R4 回查受工具超时约束，其余为纯计算；R5 本决策扩大了
  R5 在审计模式下的覆盖面。

## 验证

`tests/loop/test_audit_resume_child_thread.py`（引擎级，真实 `EnginePool` + `JsonlSessionJournalCore`）覆盖：
子 thread 调用回查完成后三层结论的内容、顺序与 actor；子 thread 在 Journal 与 transcript 投影上配对完整；
无人可问时预检即拒且只列子 thread 调用、Journal 无新增；人裁决请求带子 thread id 并以 operator actor 落账；
selected 未 started；意图未 selected；子 skill 已结束而父结果缺失；二次崩溃不重复回查、不重复落账；
两层嵌套逐层收敛且 strict verify 通过；不属于被中断派发的子 thread 仍归 others。
