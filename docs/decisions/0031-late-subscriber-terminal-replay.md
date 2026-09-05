# ADR 0031：过滤订阅为晚到订阅者补投终态

- 状态：Accepted
- 日期：2026-09-05
- 相关：[ADR 0029](0029-root-turn-serialization-single-writer.md)（每个 submission 都要拿到终结事件）；
  契约见 [audit-observability](../architecture/capabilities/audit-observability.md)。

## 背景

`AgentEngine.subscribe(submission_id)` 是 **live-only**：每次调用新建一个空队列（`_new_subscriber()`），
**不回放历史**。而 engine 只记了 `self._closed` 一个位，**不记每个 submission 的终态**。于是三种情况
的处置是分裂的：

| 订阅时机 | 原行为 |
| --- | --- |
| submit 之前 / 在飞中 | 正常收流，收到终结即收尾 ✅ |
| engine 已 close 之后 | 合成 `engine_shutdown` 终结 ✅ |
| **submission 已终态、engine 还活着** | **永远等不到任何事件** ❌ |

第三种是最常见的调用顺序（`submit()` 拿到 id 再 `subscribe()`）踩中的。turn 一快、机器一卡，
消费者就永久挂死，只能靠调用方自己的 `wait_for` 兜底——这正是 2026-09-04 CI 与本地全量跑里
那批「不同用例轮流报 `TimeoutError`」的机制来源。

现有缓解只是**调度窗口**：`Resume` / `UserMessage` 都经 `asyncio.create_task` 异步派发，
「给 `subscribe(submission_id)` 留出注册窗口」（见 agent-loop.md）。这是概率保证，不是契约保证——
窗口在负载下会关。

## 决策

engine 记住每个 submission 的**最后一条终结事件**，过滤订阅在注册之前先查一次：命中即
**补投那条真实事件**并收尾。

- **记账点**：`_emit` 内，投递之后（先保证在线订阅者拿到，再留档）。终结 kind 集合
  `_TERMINAL_KINDS = {turn_completed, turn_failed, turn_suspended}` 升为模块级单一真相，
  订阅收尾判定与记账共用——避免两处字面量漂移。
- **补投的是原事件**，不是合成的「结束了」：晚到者要知道**结果是什么**（完成/失败/挂起及其 data）。
  全局 `seq` 保持原值（seq 是事件身份，不重新分配）；`delivery_seq` 按新订阅从 0 起，
  与既有 per-subscriber 簿记语义一致。
- **补投路径不登记 `_event_subs`**：过滤订阅每 submission 至多一个订阅位，补投若占位会挤掉在线订阅者。
- **有界**：`terminal_replay_size`（默认 256）FIFO，超出淘汰最老的；`<=0` 关闭补投（回到历史行为的
  逃生口）。淘汰后退化回「等待」，不是错误——内存天花板可预测优先。
- **未知 submission 维持等待**：`subscribe` 早于 `submit` 是推荐用法，绝不能被合成终结打断。
  只有**记账命中**才补投。
- 同一 submission 重复终结（如 `turn_suspended` 后 Resume 又跑出 `turn_completed`）以**最后一条**为准。

## 后果

### 正向

- `submit()` → `subscribe()` 这一最自然的顺序不再有挂死风险，业务侧不必再靠自己的超时兜底。
- 三种订阅时机的处置统一：在飞收流 / 已终态补投 / engine 已关合成，不再有「什么都拿不到」的第四种。
- 测试层面消掉一整类间歇红（等事件等到自己的 `wait_for` 到期）。

### 代价

- 每 engine 多驻一份有界终态表（默认 256 条 `EventMsg`）。
- 超过窗口的**很**晚订阅者仍会等待——这是有意的降级，不做无界缓存。
- 补投只给终结事件，不回放中间过程。要完整历史请用 `subscribe_all` 并在 submit 前建立订阅，
  或读 durable transcript。

## 被否决方案

1. **回放全部历史事件**：内存无界，且与 EventMsg「可丢、不保证送达」的内核语义（R4）冲突。
2. **合成一条通用终结（如 `engine_shutdown` 同形）**：晚到者拿不到真实结果，只知道"结束了"，
   会把成功与失败混为一谈。
3. **让 `submit()` 返回时保证订阅已建立**（同步注册）：把订阅耦合进提交路径，`submit` 不再纯粹，
   且多订阅者场景无法表达。
4. **只在文档里要求「必须先 subscribe 再 submit」**：把内核缺口转嫁给每个调用方，
   且与现有异步派发的既定用法冲突。

## 验证

- `tests/loop/test_late_subscriber_terminal.py`：9 个用例——已终态补投真实事件（含 data / seq 一致）、
  三种终结 kind 均记账、`delivery_seq` 从 0、未知 submission 仍等待、订阅早于 submit 行为不变、
  有界淘汰、`terminal_replay_size=0` 关闭。
- 归因探针 `chore/flake-triage` 分支的 `test_probe_late_subscribe_after_terminal`（改前确定性失败）
  现已通过。
