# Capability: midturn-input-steering

## Purpose

让业务在一个 turn **运行期间**把增量用户输入投进去（steering），在该 turn 的下一个迭代边界并入 prompt，而不中止 turn、不丢已跑迭代——替代「只能 `CancelTurn` 重来」。无活跃 turn 时退化为落历史不起新 turn。

参照：codex `session/inject.rs`（`inject_if_running` / `inject_no_new_turn`）+ `session/input_queue.rs`（迭代边界排空 pending）。第三轮对比分析 P0 缺口 B1。

实现：`src/taifeng/loop/submission.py`（`InjectUserInput` Op）、`src/taifeng/loop/engine.py`（`_PendingTurn.pending_input` + 主循环路由）、`src/taifeng/loop/turn.py`（`pending_input` 字段 + `_drain_pending_input` + 迭代边界调用）、`src/taifeng/loop/event.py`（`UserInputInjected`）。
变更提案：`openspec/changes/midturn-input-steering/`。

## 架构前提

`UserMessage` 经 `asyncio.create_task(self._run_turn_for(...))` 派发——**turn 跑在独立 task，不阻塞 Op 主循环**。因此 turn 运行期间主循环仍能消费新 Op，`InjectUserInput` 可旁路投给正在跑的 TurnRunner，无需把 turn 额外 task 化。

## 数据契约

### `InjectUserInput` Op（`loop/submission.py`）
| 字段 | 含义 |
| --- | --- |
| `submission_id` | 目标活跃 turn 的 submission id |
| `text` | 注入的用户文本 |

区别于 `InjectSystemMessage`（注 system 注记、永不影响活跃 turn）。

### 共享 `pending_input` 队列
`_PendingTurn.pending_input: list[ResponseItem]` 与对应 `TurnRunner.pending_input` 是**同一 list 引用**（`_run_turn` 构造 TurnRunner 时从 `self._pending[submission_id]` 取）。engine 主循环 append、runner 迭代边界 drain。同一 event loop 协作式调度，append/drain 都在无 await 的同步段完成 → 无需锁。

**ADR 0029 单写者**：有活跃根 turn 时 `InjectSystemMessage` 也走这条队列（item kind=`system_injection`），engine MUST NOT 直接写 root history；无活跃 turn 时才由 engine 直接落史。事件构造集中在 `loop/injection.py::injection_event`。

### `UserInputInjected` 事件（`loop/event.py`，`kind="user_input_injected"`）
| 字段 | 含义 |
| --- | --- |
| `data.submission_id` | 目标 turn |
| `data.delivered` | `true`=已并入活跃 turn 的 history（drain 时发）；`false`=无活跃 turn 落历史未起新 turn，或 `reason="turn_ended"`（turn 退出时残留落史，未进入本 turn prompt） |
| `data.text_preview` | 文本前 80 字 |
| `data.reason` | `null` 或 `"turn_ended"` |

`SystemMessageInjected`（`kind="system_message_injected"`）字段同形，对应 `InjectSystemMessage`。

## 行为契约

### Requirement: 运行中 turn 接收增量输入
- **WHEN** turn 运行中 `submit(InjectUserInput{submission_id, text})`
- **THEN** text 投进该 turn 共享 pending 队列；`TurnRunner._drain_pending_input` 在下一迭代边界把它转 user_message 并入 history（已跑迭代保留）；emit `user_input_injected{delivered:true}`

### Requirement: 无活跃 turn 不起新 turn
- **WHEN** 目标 submission 无活跃 turn
- **THEN** text 作为 user_message 落历史 + 持久化；不创建 TurnRunner；emit `user_input_injected{delivered:false}`

### Requirement: 边界保护 tool 配对
- **WHEN** turn 正在派发一批 tool call
- **THEN** 注入推迟到该批闭合后的迭代边界并入（drain 点在迭代循环顶部、`_maybe_compress(pre_turn)` 前），history 不出现配对孤儿

### Requirement: Cache 友好 + 尊重取消 + 可 resume
- 注入作为 tail 追加，不动 cache anchor 之前的 head；目标 turn 已取消 → 迭代边界 drain 不并入（`cancel.is_cancelled` 守卫）；turn 以任何方式退出（取消 / 异常 / 挂起）时 runner SHALL 在终态事件之前把残留 pending 全部落史（`_drain_pending_input(residual=True)`，事件 `delivered:false, reason:"turn_ended"`），engine `_drain_residual_injections` 再兜底一次；注入 MUST NOT 丢（R5）。

#### Scenario: 已取消 turn 的注入由 engine 收尾落史
- **WHEN** `InjectUserInput` 后 turn 被 `CancelTurn`
- **THEN** 注入文本以 user_message 出现在热与冷 history 中；事件 `user_input_injected{delivered:false, reason:"turn_ended"}` 在 `turn_completed` 之前发出

### Requirement: 晚到注入收尾不丢（R5）
注入在 turn **最后一轮采样期间**到达（`delivered=true`，但该轮无后续 tool call、无下一迭代 drain）时，turn 正常退出前 SHALL 补一次 drain 把它落历史——否则 pending 随 turn 结束被丢弃，违反 R5。

#### Scenario: 最后一轮期间注入
- **WHEN** 注入投进活跃 turn 的 pending，但该 turn 当轮即结束（无后续迭代）
- **THEN** turn 收尾（正常退出路径，`run()` 主循环后）SHALL drain 剩余 pending 落历史 + 持久化
- **AND** 注入不丢（虽未影响本 turn 采样，下个 turn 可见）

> 此缺陷由真实 LLM 验证（`examples/real_llm/p0_verify.py`，单轮 turn）逼出，mock 多轮 e2e 漏掉；回归测试 `test_late_inject_not_lost_at_turn_end`（含反证：禁用收尾 drain 即 FAIL）锁定。

## R1–R5 影响

- **R1**：✅ 注入中性 `ResponseItem`，无业务概念。
- **R2**：✅ pending 只追加 tail，不动 head。
- **R3**：✅ `user_input_injected` / `system_message_injected`（delivered 真假 + reason）。
- **R4**：✅ drain 前 `cancel.is_cancelled` 守卫；不绕过 CancellationToken。
- **R5**：✅ 并入 / 落历史均经 store.append 持久化。

## 能力边界

mid-turn steering 的生效**依赖 turn 持续时间 > 注入往返延迟**：注入经 `submit` → Op 主循环 → 活跃 turn 的 pending 队列。若注入到达时 turn 已结束（`_pending` 已 pop），按 spec 退化为 `delivered=false`（落历史不起新 turn）。

- 真实 LLM turn 每轮采样秒级延迟，注入窗口充足。
- 极快 turn（纯 mock 无延迟）注入可能错过窗口 → `delivered=false`。这是**正确退化**（spec「无活跃 turn」场景），非缺陷：steering 的语义本就是「turn 还在跑才能 steer」。
- 业务若要「保证被下一 turn 看到」，应在 turn 结束后用 `UserMessage` 起新 turn，而非 steering。

## 测试

`tests/loop/test_midturn_steering.py`：`_drain_pending_input` 单元（pending→history+store+清空+事件）、engine 路由 `delivered=true`（白盒注册活跃 `_PendingTurn`）/ `delivered=false`（无活跃 turn 落历史不起新 turn）、**`test_inject_consumed_by_running_turn_e2e` 真端到端**（多轮 turn + `SimTurn.delay_seconds` 模拟 LLM 采样延迟，确证注入被运行中 turn 在后续迭代 drain 消费、文本真进 history；连跑 5 次稳定）。
