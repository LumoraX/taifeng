# ADR 0107：重放重现录制里的挂起与 writer 接管

- 状态：Accepted
- 日期：2026-09-30
- 关联：Amends #0105；[journal-replay](../architecture/capabilities/journal-replay.md)；ADR 0097（挂起与恢复）、0054

## 背景

ADR 0105 的整条重放在 sim 下成立，拿一份真实 LLM（codex / Responses 协议）的录制来重放时，
「挂起 → 释放 → 新 Engine 接管 → `Resume`」这一段在接管后的第一次请求上分叉。逐个对照录制与重放的
请求，差异有三处，都不是模型行为的差异：

1. **结果对话项缺采样归属**。等过人的调用在 `Resume` 之后结算，写下的 `function_call_output` 没有带
   `origin_llm_sample_id`；不经挂起、当场结算的结果带。Responses 协议按它把结果与发出调用的那次采样
   归到同一组，缺了它请求里这一项的形状就不一样。这是 ADR 0097 实现的缺口，与重放无关——真实运行
   里同样存在，只是 provider 容忍。
2. **跳过挂起改变了请求的形状**。ADR 0105 决策 4 让被替换的工具直接交回录制的结果，对应的 `Resume`
   记为 `not_needed`。但挂起前后是两个 turn：录制里结果出现在 `Resume` 那个 turn 的第一次请求里，
   跳过挂起则出现在原 turn 的下一次采样里，请求序列对不上。
3. **接管丢掉缓存断点**。新 Engine 不信任上一个进程留下的 provider cache（`_cache_anchor_index` 从 -1
   起），接管后第一次请求不带 `cache_breakpoints`；同一个 Engine 连续跑则带。`cache_breakpoints` 是
   请求摘要的一部分（ADR 0054）。

命中 ADR 0017 规则①。

## 决策

1. **等过人的调用的结果带上发出该调用的那次采样的 id**。`awaited_convergence` 从 history 里的
   `function_call` 项取 `llm_sample_id`，作为结果项的 `origin_llm_sample_id`；答复后重跑与拒绝后结算
   两条路径一致。
2. **录制里停下等人的调用在重放里也停下**（推翻 ADR 0105 决策 4 的 `not_needed` 处理）。
   `recorded_tool_calls` 从 `suspension` 对话项取出每次调用的待答请求；回放 handler 第一次被调用时
   重新抛出同一个请求（同一个请求 id），turn 挂起；录制的 `Resume` 结清它，获批的调用再次被调用时才
   交回录制的结果。被拒的调用不会再次被调用，内核照常按拒绝结算。录制结束时仍在等的调用状态为
   `awaiting`，被要求交回结果时 `ReplayUnsupportedError`。
3. **writer 接管是录制序列的一步**。`recorded_submissions` 为每条 `writer_takeover` 产出一步
   `takeover`；`replay_session(..., reopen=)` 在这一步调用 `reopen(engine)` 换成接管同一 Session 的
   新 Engine。回放客户端与工具台账由调用方跨 Engine 沿用。不给 `reopen` 时这一步记为
   `takeover_skipped`、沿用原 Engine——其后的请求可能因缓存断点不同而分叉，报告里能看到原因。

## 不做

- **把 `cache_breakpoints` 从匹配里剔除**。它不改变模型的输出，但它是内核的行为（压缩与缓存锚点
  的结果），剔除后「缓存锚点算错」这类回归就重放不出来。
- **接管时恢复缓存锚点**。跨进程不信任 provider cache 是既有约定（`engine-resume-by-thread-id`）；
  为了让重放好对而改变真实运行的缓存行为是本末倒置。
- **重现崩溃式接管**。录制里没有 `session_detached` 的接管来自进程崩溃；公开接口只能优雅释放，
  而没有挂起的 Session 优雅释放即终结，无法再被接管。此时 `reopen` 抛错，重放停在这一步并报告。

## 影响

- R1：无业务概念。
- R2：不改变缓存行为；重放如实重现接管后的无断点请求。
- R3–R4：无影响。
- R5：无影响；重放的 Journal 里出现与录制相同的 `session_detached` / `writer_takeover`。

### 行为变化

- 审计模式下 `Resume` 之后结算的 `function_call_output` 多带 `origin_llm_sample_id`（Responses 协议
  请求里该项多一个归组字段；Chat 协议不受影响）。
- `recorded_submissions` 的结果里可能出现 `kind="takeover"` 的步骤；`replay_session` 多一个可选的
  `reopen` 参数，报告里多两种结局（`reopened` / `takeover_skipped`）。
- `RecordedToolCall` 多一个 `awaited` 字段与 `awaiting` 状态；录制里挂起过的调用在重放里不再直接
  交回结果。

## 验证

- `tests/loop/test_audit_suspension.py::test_resumed_outputs_are_grouped_with_the_sampling_that_made_the_call`；
- `tests/loop/test_journal_replay_session.py`（6 项）：挂起在重放里重现并被录制的答复结清；跨接管的
  录制给 `reopen` 后完整重放、重放的 Journal 里同样出现释放与接管；不给 `reopen` 时接管记为
  `takeover_skipped`、随后的请求分叉；`reopen` 失败时停在接管一步；
- 真实 LLM：`examples/real_llm/audit_smoke.py`（codex / gpt-5.6-luna，2026-09-30）——审批挂起 → 释放 →
  接管 → `Resume`、分离式派发、Timeline 三种视图、录后不触网重放，24 项检查全部通过；重放结局
  `turn_suspended → reopened → turn_completed → turn_completed`，LLM 与工具录制全部被消费。
