# Capability: peer-mailbox-messaging

## Purpose

同一 engine 谱系内(root thread + 全部 spawn child threads)**活体 agent 间点对点消息**——补齐 actor 通信的最后一块:此前父子只有单向 fork/join(spawn 句柄 + join-barrier),sibling 专家间、child→parent 在运行期**无法互通**。参照 codex `multi_agents_v2`(`MessageDeliveryMode{QueueOnly, TriggerTurn}` / `wait_agent` timeout 强制 / 禁止 TriggerTurn 打 root)。

第三轮对比分析 P1 缺口 D1。实现:`loop/peer_mailbox.py`(`PeerMailbox`:`deliver_peer_message` / `_wake_peer_turn` / `wait_spawn_terminal`;`_live_runners` 表由 `loop/spawn_driver.py` 的 `SpawnDriver` 单一持有,公共入口经其同名转发器暴露)、`loop/engine.py`(薄转发 + `SendToPeer` Op 分支)、`loop/submission.py`(`SendToPeer`)、`tool/builtins/{send_message,wait_peer}.py`、`loop/event.py`(4 事件)。

## 数据契约

### 寻址(D2:谱系内 thread_id,拓扑路径 deferred)

| target 取值 | 解析 |
| --- | --- |
| child_thread_id | 直接命中已登记 spawn child |
| handle_id | 经句柄表解析到 child_thread_id |
| `"parent"` | 本谱系 root thread(嵌套 spawn 的 parent 亦收敛到 root) |
| 其他 | `ValueError: unknown_peer_target`(显式失败;跨 engine 天然失败于此) |

### peer 消息形态(D3:不新增 ResponseItem kind)

`user_message` 形态,payload 标注 `source="peer"` + `from_thread=<sender_tid>`——prompt 渲染层 / 业务审计可区分 peer 消息与真实用户输入;skill 提示词应声明 peer 消息语义(业务责任)。

### `SendToPeer` Op(kind="send_to_peer")

`{target_thread_id, text, mode="queue_only"|"trigger_turn", from_thread_id(None=root)}`——与 `send_message` 工具收敛到 `engine.deliver_peer_message` 同一路径;投递失败(未知目标 / TriggerTurn 打 root)emit `EngineLog` warning(不静默)。

### 工具

- `send_message(target, text, mode)`:经 `ctx.extras["spawn_coordinator"]`(与 spawn 四工具同范式);寻址失败返回 error 结果(turn 不失败)。`parallel_safe=True`。
- `wait_peer(handle_id, timeout_seconds)`:**timeout 必填**(schema required——互等死锁无法静态防,timeout 是唯一保底);轮询句柄表至终态返回 `{status, result}`;超时返回 timeout error(turn 继续);经 `ctx.cancel` 级联取消(R4)。与 `await_skills` 分工:barrier=「登记后 turn 结束、全终态起聚合」,wait_peer=「turn 内原地等单个」。
- `wait_any(handle_ids, timeout_seconds)`:**any-of-N** 等待——`handle_ids` 中**任一**到达终态即唤醒,返回**当时全部**已终态句柄 `{settled: {hid: {status, result}}, pending: [...]}`;timeout 必填(同 `wait_peer` 的死锁保底理由);全 pending 至超时返回 timeout error(turn 继续);经 `ctx.cancel` 级联取消(R4)。`parallel_safe=True`。
- 三工具**不默认注册**——与 spawn 四工具一样经 `extra_tools` 注入 + entry skill `tool_names` 显式启用;回滚 = 不启用。

### 事件(R3,data 不含正文)

| kind | data |
| --- | --- |
| `peer_message_sent` | `{from, to, mode, delivered_via: "pending_input"\|"history", mode_downgraded, text_len, text_preview(80)}` |
| `peer_agent_woken` | `{thread_id, handle_id}` |
| `peer_wait_started` | `{handle_id, timeout_seconds}` |
| `peer_wait_resolved` | `{handle_id, outcome: "terminal"\|"timeout", status}` |
| `peer_wait_any_started` | `{handle_ids, timeout_seconds}` |
| `peer_wait_any_resolved` | `{settled_ids, pending_ids, outcome: "terminal"\|"timeout"}` |

## 行为契约

### Requirement: 双模式投递分支(D3/D4)

| 目标状态 | QueueOnly | TriggerTurn |
| --- | --- | --- |
| 运行中(live runner 在册) | 投其 `pending_input`(B1 steering 同一队列,下迭代边界 drain 并入) | **降级 QueueOnly**,`mode_downgraded=true`(不打断采样) |
| 空闲(spawn child 终态) | `store.append` 即时落史(R5) | 落史 + `_build_child_runner` 续跑范式唤醒新 detached turn,emit `peer_agent_woken`,K1 slot 重新占用(finally 释放),`_finalize_spawn` 收敛终态(再挂起可再 resume) |
| suspended(HITL) | 仅落史 | 仅落史,**不唤醒**(挂起只能由 Resume 解除;落史后随续跑可见) |
| root thread | 有活跃 turn 投 pending_input,否则落 engine 历史 | **拒绝**(`trigger_turn_root_forbidden`——root 由用户驱动,对标 codex) |

### Requirement: live runner 登记

`SpawnDriver._live_runners: {child_thread_id → TurnRunner}`,四条驱动路径(首发 `_drive_spawn` / `resume_spawn` / `resume_spawn_nested` 链根重跑 / `_drive_woken_turn`)run 前登记、finally 弹出——「运行中」判定 = 句柄 running 且 live runner 在册(状态与 runner 不一致的瞬间窗口落史兜底,不丢消息)。

### Requirement: wait_any 的 any-of-N 唤醒语义

`wait_any(handle_ids, timeout_seconds)` 补齐等待原语的中间档——现有两端是「等一个」(`wait_peer`)与「等全部」(`await_skills` barrier),缺的这档使编排者面对错峰完成的 N 个子任务时只能盯死一个或等最慢的,想「谁先好先处理谁」只能让 LLM 轮询 `join_skill`(每轮烧一次采样)。对标 codex `wait_agent`(wait for a mailbox update from **any** live agent)。

- **唤醒条件**:`handle_ids` 中 SHALL 至少一个进入终态(done / error / cancelled)。
- **唤醒时收全**:SHALL 返回唤醒当时**全部**已终态句柄的 `{status, result}`,其余进 `pending`。同批多个完成 SHALL NOT 要求调用方反复调用。
- **已终态即返**:调用时若已有终态句柄,SHALL 立即返回不空转(同 barrier「登记后立即检查一次」范式)。
- **`suspended` 不算唤醒条件**:与 `wait_peer` / barrier 的终态口径一致;HITL 挂起期的等待由 `timeout_seconds` 兜底。
- **未知句柄显式失败**:任一 handle_id 未注册 SHALL raise `ValueError("unknown_spawn_handle: <id>")`,SHALL NOT 静默跳过。
- **空 handle_ids 拒绝**:SHALL raise `ValueError`——空集永不可能被满足,等下去必然只能超时。
- 轮询粒度与取消语义 SHALL 与 `wait_spawn_terminal` 一致(50ms;每圈 `raise_if_cancelled`)。

#### Scenario: 任一终态即唤醒

- **GIVEN** 三个 spawn 句柄,其中一个先跑完、两个仍 running
- **WHEN** 调用 `wait_any([a, b, c], timeout_seconds=5)`
- **THEN** SHALL 在该句柄终态后即返回,`settled` 含它、`pending` 含另两个,且 SHALL NOT 等到全部完成

#### Scenario: 同批多个终态一次收全

- **GIVEN** 两个句柄在同一轮询周期内均已终态
- **WHEN** `wait_any` 唤醒
- **THEN** `settled` SHALL 同时含两个,调用方无需二次调用

#### Scenario: 全 pending 至超时

- **GIVEN** 全部句柄在 `timeout_seconds` 内均未终态
- **THEN** SHALL 返回 `outcome="timeout"`,`settled` 为空,turn SHALL NOT 失败

#### Scenario: 未知句柄显式拒绝

- **WHEN** `handle_ids` 含未注册的 id
- **THEN** SHALL raise `ValueError("unknown_spawn_handle: ...")`,SHALL NOT 跳过该 id 继续等其余

### Requirement: 与既有机制正交

join-barrier 语义不受影响(peer 消息不计入 barrier 条件);kill_spawn 取消 token 表照常覆盖唤醒 turn(`peer_wake:<handle>`);K1 配额、K2 token 上限、max_iterations 是消息风暴的既有兜底(内核不做限流——「过多」是业务语义,R1)。

## R1–R5 影响

- **R1**:✅ 寻址/投递/唤醒全是机制;无业务概念;限流策略业务自决。
- **R2**:⚪ 不触压缩;pending_input 注入沿用 steering 既有 cache 性质。
- **R3**:✅ 6 事件(含 `peer_wait_any_started` / `peer_wait_any_resolved`)+ console 专用渲染;Op 失败 EngineLog 告警。
- **R4**:✅ wait 轮询每圈 `raise_if_cancelled`;唤醒 turn 挂根取消树(`peer_wake:` 子 token);kill_spawn 可精确取消。
- **R5**:✅(有界声明)空闲投递即时 `store.append` 持久;**运行中投递的瞬态窗口与 steering pending_input 完全相同**——turn 收尾必 drain 落史,进程崩溃时在途丢失语义同 UserMessage(D3 拍板:不为此引入独立持久 mailbox 的恢复扫描/去重成本)。

## 测试

`tests/loop/test_peer_messaging.py`(17):事件/Op 形态、QueueOnly 空闲落史 R5 + 事件契约、handle_id/"parent" 寻址、未知目标显式 error、TriggerTurn root 拒绝、空闲唤醒(句柄重回 done + 新 result)、运行中降级(gate 工具钉住运行态 → pending_input → drain 并入)、suspended 不唤醒、wait_peer 终态/超时/取消级联、SendToPeer Op 同路径、旗舰 e2e(LLM spawn → send_message trigger_turn 唤醒 → 专家产出补充结论);wait_any 四例(任一终态即唤醒 + 事件形态 / 同批多终态一次收全 + 已终态立即返回 / 全 pending 超时 + 空集与未知句柄显式抛 / 取消级联)。

> demo:`examples/peer_messaging/demo.py`(mock 可跑)。拓扑路径寻址(`sibling:<skill_id>`)与跨 engine 通信 deferred(见 ADR/design D1-D2)。
