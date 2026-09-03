# ADR 0029：根 turn 排队串行，在飞期间 root history 单写者，终结信号完整

- 状态：Accepted
- 日期：2026-09-03
- Amends：ADR 0025（audited accepted token 的 application 时机）
- 关联：[agent-loop 活文档](../architecture/agent-loop.md) §root gate；[midturn-input-steering 契约](../architecture/capabilities/midturn-input-steering.md)；openspec change `wave2a-root-turn-truth`

## 背景

2026-09-03 全系统审查用 SimClient 复现了三条根 turn 层面的缺陷：

1. turn 进行中提交 `InjectSystemMessage`：engine 直写 `_history` + store，turn 结束时 `_writeback_turn_runner` 用 runner 快照整表覆盖 → 热 history 少一条、冷 history 有 → 进程重启后 LLM「突然」看到那条注入。同根因覆盖 `CompactNow` / `ThreadRollback` / `Rewind` 与两条 `UserMessage` 连发。
2. `InjectUserInput` 后 `CancelTurn`：注入只入队不落盘，turn 被取消后队列随 `_PendingTurn` 丢弃，事件却报 `delivered:true`。文档「engine 收尾落历史，不丢」为假。
3. Shutdown 后按 submission_id 过滤的订阅永久挂死：shutdown 只投 firehose，过滤订阅的 id 不匹配；operation 崩溃只写日志、无终结事件、`_pending` 留幽灵。

三者根因是同一个模型缺陷：**根 turn 不串行化 + 两个写者 + 整表覆盖 + 非正常退出无终结信号**。agent-loop.md 曾把「引擎不串行化相邻 turn」写成宿主约定（等 `post_turn_hook_fired` 再提交下一轮），但引擎自身没有任何保护。

## 决策

### 1. 根 turn 排队串行（root gate）

同一 engine 同时最多运行一个根 turn。`UserMessage` / `CompactNow` / `ThreadRollback` / 根 thread 的 `Rewind` / 根 thread 的 `Resume` 进入 `asyncio.Lock`（FIFO）排队，前一个持有者到达真终态（含 post_turn hook）后按提交序执行。排队 emit `submission_queued{waiting_on}`；`_pending` 在排队前登记，`CancelTurn` 可取消排队中的 submission。`CompactNow` / `ThreadRollback` 从 actor 循环内联改为 operation。中途插话走 `InjectUserInput`（codex 语义）。

### 2. 在飞期间 root history 单写者

turn 在飞时只有 runner 写 root history。`InjectSystemMessage` 与 `InjectUserInput` 同走 runner 的 pending 队列，runner 在迭代边界落 buffer + store。热 == 冷由构造保证，不需要合并算法。turn 以任何方式退出时，runner 在终态事件之前把残留 pending 落史（`delivered:false, reason:"turn_ended"`），engine 再兜底一次。取消时已流式的 assistant 文本以 `truncated=true` 落史。

### 3. 终结信号完整

每个 submission 在任何退出路径都有终结事件：operation 崩溃 → `turn_failed{kind: <异常类名>}` + 清 `_pending`；Shutdown → 对每个过滤订阅投 `turn_failed{kind: engine_shutdown}`；engine 收敛后晚到的订阅立即得到合成终结；子 thread 续跑 turn 登记 `_pending`。

### 4. audit 模式的 application 时机（Amends ADR 0025）

accepted token 的 application（user item 进 history + 投影）**推迟到它拿到 root gate**；accept 本身（durable 准入记录）仍在提交时落盘。理由：若 application 在准入时立即发生，排队中的 B 的 user 消息会在 A 的 turn 拷快照前后不确定地出现在 A 的 prompt 里，且 transcript 变成 `A.user, B.user, A.assistant, B.assistant`。配套调整：

- actor 握手（`AuditedApplicationCheckpoint`）语义改为「交接完成」：gate 空闲时等 application 收敛（原语义）；gate 被占时登记排队即放行 actor 出队下一个 token。排队 token 之后的 application 失败走 operation 自己的 freeze / 终结路径。
- engine 收敛（release）时对仍排队的 accepted token「只应用不跑 turn」，满足「release 等 application 收敛」（ADR 0025）。
- gate 获取对「锁与任务级 raw cancel 同轮到达」做防护：任务处于 cancelling 状态一律视为未获取并归还锁——否则会在 release 期间跑起 turn，其 journal append 撞上 commit_outcome_unknown 冻结 session。

## 替代方案

- **拒绝并发**（`submission_rejected{turn_in_flight}` 交宿主重试）：最显式，但宿主要多写一层；用户否决。
- **保持并发，只把整表回写改成增量合并**：数据不丢，但两个根 turn 交错写历史、rewind 节点表与 anchor 仍互踩，只是止血；否决。
- **audit application 保持准入即应用**：改动最小，但 A 的 prompt 内容取决于时序、transcript 乱序；否决。

## 后果

- **BREAKING（行为）**：并发根 turn 不再并行跑。宿主若依赖「两条 UserMessage 并发」需改为顺序提交或用 `InjectUserInput` 插话。既有 5 个断言并发根 turn 的 audit 用例按串行语义改写。
- 新事件 `submission_queued`、`system_message_injected`；`turn_failed` 新增 kind `engine_shutdown` / `cancelled`（排队中）/ `<ExceptionName>`。
- `engine.py` 因新增 gate / 终结 / 收尾逻辑增至 ~3900 行，拆分归 Wave 4（proposal 已声明）。
- 未做（另立 change）：cache anchor 采样后推进（2c）；spawn / resume 二次驱动走 `reconstruct_logical_history`、聚合 turn 登记句柄、五份驱动收单入口（2b）。
