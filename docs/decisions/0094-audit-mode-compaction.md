# ADR 0094：审计模式放开折叠式上下文压缩与预算提示

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §15](../architecture/capabilities/session-journal-business-integration.md)；
  [context-compression](../architecture/context-compression.md)；ADR 0025（Phase 3 / 4）、0053、0083、0085

## 背景

审计模式在构造期拒绝任何压缩策略。长会话因此只有一条路：history 一直增长到上下文溢出、turn 失败。
ADR 0025 的 Phase 3 约定接入后「保持 compaction 兼容」，此前未做。

另有一处既有缺陷：预算提示在审计模式下直接写投影 store，绕过了 Journal——投影的唯一写者应是 projector，
而这条提示确实进了模型的上下文，却不在 Journal 里。

命中 ADR 0017 规则①。

## 决策

1. **只放开折叠式压缩**：结果是「一段 history 被一条 `compacted` 条目替代」。被替代的条目仍在 Journal 里，
   摘要条目也在，逻辑 history 可由两者重放得到。原地改写条目的策略（裁剪、驱逐、落盘）改写后的条目
   不进 Journal，放开它们就得为「条目的新版本」另立记录与重放规则，本次不做。
2. **策略自己声明支持**（类属性 `audit_support`）。内核不去猜一个策略是不是折叠式；
   没有声明的第三方策略默认不可用。
3. **压缩结论是独立的 record（`context_compacted`），与摘要条目同批提交**。只落摘要条目的话，
   策略、时机、压缩前后的占用、对缓存的影响都无从查证。
4. **调用模型的策略经内核提供的会话调用**（`CompressionContext.model_session`）。审计模式下每次 LLM
   调用都要先落请求、后落 checkpoint；策略持有的客户端绕开了这条路径。非审计模式下该字段为空，
   策略照旧用自己的客户端。
5. **压缩发起的 LLM 调用用保留的 iteration 区间**（从 1 000 000 起）。沿用既有的 LLM operation 文法，
   attempt、checkpoint、回放都不必认识新的 operation 种类；`context_compacted` 回指这些调用。
6. **摘要调用失败也落账**。调用已经发生并产生了费用；压缩没成不改变这个事实。
7. **先落账，ack 后才改 hot history**，与其余 effect 一致。
8. **只在采样之间压缩**。手动压缩需要对应的已落账 Op；溢出自愈要对同一次 LLM 调用重采样，
   而审计下每个 LLM operation 的 attempt 序号不跨采样延续，重采样会与首次的 record id 相撞。
   两者都留给 Op 与 retry generation 接入之后。
9. **回写 engine history 时排除被折叠的条目**。既有的回写是「只增不减」的合并，折叠掉的条目会被并回来。
   runner 记下被折叠的 id，合并时跳过它们，其余条目照旧按完整身份核对。
10. **预算提示一并落账**（`budget_hint_injected` + `system_injection` 对话项）。`system_injection` 只接受
    `budget_hint` 这一个来源：截断类 marker 会改写 history，不能借这条路进 Journal。

## 替代方案

- **把压缩结论写进摘要条目的 metadata，不另立 record**：对话项是「模型看到了什么」，
  压缩结论是「内核做了什么」，混在一起后 Timeline 无法把压缩当作一个事件呈现。
- **为压缩的 LLM 调用新增 operation 种类**（`{turn}:compaction:{n}:llm:{k}`）：文法、record id 校验、
  attempt 观察者、resume 扫描、回放都要跟着认识它，收益只是 identity 更好看。
- **审计模式下直接不让调用模型的策略运行**：默认压缩就是摘要式，禁掉它等于审计模式只能用滑窗丢弃。
- **回写时用 Journal 重放的结果整体替换 engine history**：最干净，但每轮结束都要读一遍 Journal；
  契约里已把「以 Journal seq 建立权威回写」列为后续工作，本次不提前做。

## 后果

- 新增 record：`context_compacted`、`budget_hint_injected`；对话项新增 `compacted`、`system_injection` 两种 kind；
  operation 文法新增 `{turn_id}:compaction:{n}`、`{turn_id}:budget_hint:{n}`。
- `CompressionContext` 多 `model_session`，`CompressionResult` 多 `strategy`（由协调器填写），
  `merge_audited_history` 多 `superseded`；都有默认值。
- 审计静态门对压缩协调器改为按策略声明放行；`SlidingWindowStrategy`、`HandoffCompactionStrategy` 声明了支持。
- 行为变化（缺陷修复）：审计模式下的预算提示改经 Journal 落账，不再直写投影。
- resume 重建 root history 时按压缩标记重放。
- 未覆盖：手动压缩、溢出自愈、原地改写的策略、后台压缩、ContextEngine；子 thread 上的压缩沿用同一实现，
  但被中断派发的恢复（ADR 0076）不重放子 thread 的压缩标记——恢复不续跑子 skill，不需要它的逻辑 history。
- R1：无业务概念。R2：压缩结果照旧声明缓存影响，并随 record 落账。R3：既有压缩事件不变。
  R4：压缩的 LLM 调用绑定所属 turn 的取消 token。R5：压缩成为 Journal 里的事实，恢复后逻辑 history 一致。

## 验证

`tests/loop/test_audit_compaction.py`，条目见契约 §15.5。
