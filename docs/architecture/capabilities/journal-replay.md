# Journal 录后重放能力契约（replay 模式）

> 状态：Experimental。关联 ADR 0054（LLM 回放）、ADR 0105（工具回放与整条重放）、ADR 0107（挂起与接管）。依赖
> `session-journal-business-integration`。LLM 回放的匹配规则见 [llm-client.md §Journal 确定性回放](../llm-client.md)。

## 1. 范围

一个录制过（审计模式）的 Session 在不触网、不碰外部系统的情况下整条重放：

| 部分 | 入口 | 数据源 |
| --- | --- | --- |
| LLM 回复 | `JournalReplayClient.from_records(records)` | `llm_request_committed` / `llm_response_committed` |
| 工具结果 | `replay_tools(tools, recorded_tool_calls(records))` | `tool_intent_committed` / `tool_outcome_committed`（附件取 `function_call_output` 对话项） |
| 提交序列 | `replay_session(engine, recorded_submissions(records), reopen=...)` | `submission_accepted`（用户消息）/ `resume_accepted`（答复）/ `writer_takeover`（接管边界） |

内核编排工具（`KERNEL_TOOLS`：`call_skill` / `read_skill` / `search_skills` / `spawn_skill` / `kill_skill` /
`join_skill` / `wait_peer` / `wait_any` / `await_skills` / `send_message` / `request_user_input`）不从录制取
结果，照常运行：它们的效果——子 turn、句柄、消息——由内核自己重现，子 turn 里的 LLM 调用照样从录制匹配。

## 2. 工具回放

`RecordedToolCall`：`intent_record_id` / `call_id` / `name` / `arguments_hash`（有效参数的 canonical hash）/
`status` / `output` / `attachments` / `error_code` / `awaited`（录制里这次调用停下等人时的待答请求）。

- 匹配按（工具名，规范化后的有效参数），同名同参的多次录制按录制顺序消费；找不到 →
  `ReplayDivergenceError`（含剩余 / 已消费计数），turn 以 `turn_failed` 终止。
- `success` 还原为 `ToolResult.ok(output, attachments=...)`；`error` / `rejected` / `cancelled` 还原为
  `ToolResult.error(output, reason=<stable_error.code 或 status>)`；`unknown` 不能回放
  （`ReplayUnsupportedError`）。
- 录制里停下等人的调用（`awaited` 非空，取自 `suspension` 对话项）在重放里也停下：handler 第一次被
  调用时重新抛出录制的那个待答请求（同一个请求 id），turn 挂起；挂起被结清、这次调用再次被调用时才
  交回录制的结果。被拒的调用不会再被调用，由内核按拒绝结算。录制结束时仍在等的调用 `status` 为
  `awaiting`，没有可交回的结果（`ReplayUnsupportedError`）。
- 被替换的工具保留全部元数据（名字、schema、审计声明），只换 handler；`ToolReplayLedger.consumed` /
  `remaining` 供断言新运行是否少调 / 多调了工具。

## 3. 整条重放

`recorded_submissions(records)` 给出 root thread 上的用户消息（全文、附件）、`Resume`（答复原文）与
writer 接管的边界（`kind="takeover"`，一条 `writer_takeover` 一步），按落账顺序。
`replay_session(engine, submissions, step_timeout=30, reopen=None)`：

- 每一步提交后等 root turn 停下（完成、失败、挂起）再送下一步；
- `Resume` 只在 Engine 有活跃挂起时送入，否则记为 `not_needed`（挂起没有在重放里重现，例如重放用的
  权限策略比录制时宽）；
- 接管一步调用 `reopen(engine)`：调用方释放当前 Engine、返回接管同一 Session 的新 Engine（通常是
  关掉 pool、重建、`get_or_create(resume_thread_id=)`），结局 `reopened`；回放客户端与工具台账跨
  Engine 沿用同一份。不给 `reopen` 时记为 `takeover_skipped`、沿用原 Engine；`reopen` 抛错则重放
  停在这一步；
- 分叉即停：某一步以 `ReplayDivergenceError` / `ReplayUnsupportedError` 结束，或准入被拒，
  `ReplayReport.diverged_at` 指向那一步；
- 报告每一步：录制的 submission id（接管一步是那次接管的 operation id）、种类、重放里的
  submission id、结局（`turn_completed` / `turn_suspended` / `not_needed` / `reopened` /
  `takeover_skipped` / 失败的异常类名）与错误文本。

重放不比较对话内容：内核的行为由 LLM 与工具的回复决定，两者都来自录制，新一轮运行走了录制里
没有的路时匹配失败就是分叉。

## 4. 确定性的前提

- 派发句柄由（turn 序号，调用 id）派生（`sp_<hash>`），录制与重放相同；审计模式下子 thread id 由
  （Session id，句柄）派生，重放用同一个 Session id、独立的 Journal 根即可得到同样的 thread id。
  `join_skill` / `wait_peer` 等工具的结果里带着这些 id，它们进入模型看到的上下文。
- root thread id 由 bootstrap 随机分配，重放与录制不同；派生自它的标识（peer 消息的 `from_thread`）
  进入上下文时会分叉。
- 直接调 `engine.spawn_skill()` 不给 `handle_id` 时句柄随机。
- 接管是请求形状的一部分：新 Engine 不信任上一个进程留下的 provider cache，接管后第一次请求不带
  `cache_breakpoints`，而缓存断点在请求摘要里。录制跨过接管时要给 `reopen`，否则接管后的请求分叉。
- 挂起前后是两个 turn：结果出现在 `Resume` 那个 turn 的第一次请求里。重放因此必须重现挂起，不能
  直接交回录制的结果。

## 5. 边界

- 重放的 Engine 通常也在审计模式下运行（LLM 客户端外再套 `AttemptObservableClientAdapter`），于是重放
  本身留下一份新的 Journal，可与录制对照。
- 不重放 `CancelTurn`、`Shutdown` 等控制类提交。
- 崩溃式接管（录制里接管之前没有 `session_detached`）无法经公开接口重现：没有挂起的 Session 优雅
  释放即终结。此时 `reopen` 抛错，重放停在接管一步。
- 图片附件随 `function_call_output` 还原；文件附件只在用户消息里，随 `submission_accepted` 还原。

## 6. 验收

录制一段含工具调用与派发的对话后重放：工具不执行、结果逐字来自录制、派出去的 worker 在后台重放完成、
LLM 与工具录制全部被消费；改动一条用户输入后第一步即分叉并停在那一步；录制里的挂起在重放里重现、
由录制的答复结清，对话走到同一处；跨接管的录制给 `reopen` 后完整重放，不给则接管记为
`takeover_skipped`、随后分叉；`reopen` 失败停在接管一步。

真实 LLM：`examples/real_llm/audit_smoke.py`（codex / Responses，2026-09-30）录制一段含审批挂起、释放、
接管、`Resume` 与分离式派发的对话后不触网重放，无分叉，LLM 与工具录制全部被消费。
