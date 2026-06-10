# Capability: peer-mailbox-messaging

## Purpose

同一 engine 谱系内(root thread + 全部 spawn child threads)**活体 agent 间点对点消息**——补齐 actor 通信的最后一块:此前父子只有单向 fork/join(spawn 句柄 + join-barrier),sibling 专家间、child→parent 在运行期**无法互通**。参照 codex `multi_agents_v2`(`MessageDeliveryMode{QueueOnly, TriggerTurn}` / `wait_agent` timeout 强制 / 禁止 TriggerTurn 打 root)。

第三轮对比分析 P1 缺口 D1。实现:`loop/spawn_driver.py`(`deliver_peer_message` / `_wake_peer_turn` / `wait_spawn_terminal` / `_live_runners`)、`loop/engine.py`(薄转发 + `SendToPeer` Op 分支)、`loop/submission.py`(`SendToPeer`)、`tool/builtins/{send_message,wait_peer}.py`、`loop/event.py`(4 事件)。

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
- 两工具**不默认注册**——与 spawn 四工具一样经 `extra_tools` 注入 + entry skill `tool_names` 显式启用;回滚 = 不启用。

### 事件(R3,data 不含正文)

| kind | data |
| --- | --- |
| `peer_message_sent` | `{from, to, mode, delivered_via: "pending_input"\|"history", mode_downgraded, text_len, text_preview(80)}` |
| `peer_agent_woken` | `{thread_id, handle_id}` |
| `peer_wait_started` | `{handle_id, timeout_seconds}` |
| `peer_wait_resolved` | `{handle_id, outcome: "terminal"\|"timeout", status}` |

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

### Requirement: 与既有机制正交

join-barrier 语义不受影响(peer 消息不计入 barrier 条件);kill_spawn 取消 token 表照常覆盖唤醒 turn(`peer_wake:<handle>`);K1 配额、K2 token 上限、max_iterations 是消息风暴的既有兜底(内核不做限流——「过多」是业务语义,R1)。

## R1–R5 影响

- **R1**:✅ 寻址/投递/唤醒全是机制;无业务概念;限流策略业务自决。
- **R2**:⚪ 不触压缩;pending_input 注入沿用 steering 既有 cache 性质。
- **R3**:✅ 4 事件 + console 专用渲染;Op 失败 EngineLog 告警。
- **R4**:✅ wait 轮询每圈 `raise_if_cancelled`;唤醒 turn 挂根取消树(`peer_wake:` 子 token);kill_spawn 可精确取消。
- **R5**:✅(有界声明)空闲投递即时 `store.append` 持久;**运行中投递的瞬态窗口与 steering pending_input 完全相同**——turn 收尾必 drain 落史,进程崩溃时在途丢失语义同 UserMessage(D3 拍板:不为此引入独立持久 mailbox 的恢复扫描/去重成本)。

## 测试

`tests/loop/test_peer_messaging.py`(13):事件/Op 形态、QueueOnly 空闲落史 R5 + 事件契约、handle_id/"parent" 寻址、未知目标显式 error、TriggerTurn root 拒绝、空闲唤醒(句柄重回 done + 新 result)、运行中降级(gate 工具钉住运行态 → pending_input → drain 并入)、suspended 不唤醒、wait_peer 终态/超时/取消级联、SendToPeer Op 同路径、旗舰 e2e(LLM spawn → send_message trigger_turn 唤醒 → 专家产出补充结论)。

> demo:`examples/peer_messaging/demo.py`(mock 可跑)。拓扑路径寻址(`sibling:<skill_id>`)与跨 engine 通信 deferred(见 ADR/design D1-D2)。
