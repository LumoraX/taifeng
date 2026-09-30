# ADR 0100：审计模式放开 peer 消息

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §20](../architecture/capabilities/session-journal-business-integration.md)；
  [peer-mailbox-messaging](../architecture/capabilities/peer-mailbox-messaging.md)；ADR 0025、0029、0091、0098

## 背景

派发与 barrier 放开之后（ADR 0098 / 0099），审计模式下的子 skill 仍然不能和发起方说话：
`send_message` 被静态门拒绝，而 `engine.deliver_peer_message()` 这个公共 API 没有门，在审计 Session
里直接调用它，消息由 store 直接写进目标 thread，不进 Journal。

peer 消息比派发多一个难点：消息要进**别人的** thread。非审计模式下「目标空闲就直接落史」，
审计模式下这样做有两个问题——

1. **写者**：目标 thread 上可能正有 turn 在写。两个写者各自「追加记录 → 投影」，投影顺序可能与
   Journal 顺序相反，投影被判为 `sequence_regression`。
2. **对话顺序**：Journal 的顺序就是接管时重建 history 的顺序。发送方在目标的一次工具调用进行中
   写下消息，重建出的 history 里这条消息就落在调用与结果之间。

命中 ADR 0017 规则①。

## 决策

1. **发出与进入对话是两条记录，由两个写者各写一条**。发送方写 `peer_message_sent`（带消息全文），
   目标 thread 的写者写对话项（回指发出记录）。
2. **进入对话的时刻是目标 runner 的迭代边界**。那是调用与结果都已配对的位置，非审计模式的
   mid-turn steering 用的也是这个位置（ADR 0029）。
3. **root 的收件队列跟着 Session 走**。root 空闲时收到的消息留在队列里，下一个 root turn 开始时收下。
   不在 root 空闲时立即写进对话：那一刻可能有已经准入、还在排队的用户消息，它们的对话项序号在前、
   应用在后，此时插入会把顺序弄乱。
4. **已经结束的子 thread 不接受消息**。它有 `thread_terminal`；终态之后再往里写，或者把它唤醒重跑，
   都让「终态」不再是终态。发送方得到 `peer_target_not_running`，由它决定要不要重新派发。
5. **turn 收尾之后、终态之前到达的消息照样写进对话**。它已经落过发出记录，thread 还没有终态；
   不写就成了一条「发出了、永远不会到」的消息。
6. **接管时未进入对话的消息回到 root 的收件队列**，由发出记录重建。发给子 thread 的不再投递：
   那个子 thread 在接管时被落为 `cancelled`。
7. **`SendToPeer` Op 不放开**。它是业务从外部注入消息的入口，准入语义与 `UserMessage` 同类
   （要有 `submission_accepted`），不是 agent 之间的消息。

## 不做

- **唤醒**（`trigger_turn` 打空闲的子 thread）：见决策 4。
- **`SendToPeer` Op**：见决策 7。
- **Session 终结时把收件队列里的消息写进 root 的对话**：没有 turn 会再读它们；发出记录已经在。

## 影响

- R1：无业务概念；消息内容对内核不透明。
- R2：消息从对话尾部进入，不动 head。
- R3：`peer_message_sent` 事件不变；`delivered_via` 多一个取值 `inbox`（仅审计模式）。
- R4：不引入新的等待。
- R5：接管后未进入对话的消息不丢，且只进入对话一次。

### 行为变化

- 审计 Session 里的 `engine.deliver_peer_message()` 现在落账，并按 §20.3 投递。
- 审计模式的静态门放行 `send_message`。
- Journal 的 `user_message` 对话项可以带 `source = "peer"` 与 `from_thread`。
- 非审计模式无变化（runner 退栈时注销 live 登记的代码搬到 `PeerMailbox.retire_runner`）。

### 已知边界

- 排队的用户消息在审计模式下的对话顺序问题（准入时落账、拿到 root gate 时才应用）与本 ADR 无关，
  是既有缺陷，另行处理。

## 验证

`tests/loop/test_audit_peer.py`（5 项）。全量 `pytest tests/` 通过；ruff 门禁与 `mypy src/` 清零。
真实 LLM 台账随集成一并刷新。
