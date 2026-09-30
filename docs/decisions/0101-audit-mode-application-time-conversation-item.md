# ADR 0101：审计模式下用户消息的对话项在应用时落账

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §7、§13.4](../architecture/capabilities/session-journal-business-integration.md)；
  ADR 0025、0029、0053
- 修正：ADR 0025 的用户入口记录语义（`submission_applied` 与对话项从准入批次移到应用批次）

## 背景

审计模式下，`UserMessage` 的准入把三条记录作为一个原子批次落账：`submission_accepted`、
用户消息的 `conversation_item`、`submission_applied`。而这条消息进入对话（进 hot history、投影）
要等它拿到 root gate（ADR 0029）。

消息排在运行中的 turn 后面时，这两个时刻之间隔着那个 turn 写下的全部内容。实测（两条消息，
第二条在第一个 turn 的 LLM 调用途中提交）：

| | 顺序 |
| --- | --- |
| Journal（按序号） | 一、二、第一轮、第二轮 |
| hot history（模型实际看到的） | 一、第一轮、二、第二轮 |
| 投影 | 一、第一轮，然后判 `sequence_regression`，停止更新 |

后果有三：

1. **投影坏了**。第二条消息的对话项序号小于已投影的水位，投影器判为序号回退，标 stale 后不再
   接受任何内容；此后这个 Session 的 transcript 不再更新。
2. **接管时重建出的 history 是错的**。重建按 Journal 序号排，得到「一、二、第一轮、第二轮」：
   模型在第一轮从未见过「二」，重建的对话却说它见过。
3. **Journal 不再是对话的事实**。审计者按 Journal 读到的对话顺序，与实际发生的不同。

只要有消息在 turn 运行途中提交就会触发，不需要崩溃。

命中 ADR 0017 规则①。

## 决策

1. **准入与应用拆成两个批次**。准入只落 `submission_accepted`；`conversation_item` 与
   `submission_applied` 在这条消息拿到 root gate 时落账。对话项在 Journal 里的位置就是它进入对话的
   位置。
2. **准入的保证不变**：`submit()` 返回之前 `submission_accepted` 已经 durable，带着消息全文、附件与
   `turn_index`；队列里不存在未准入的 submission。
3. **对话项由准入记录确定**。id 为 `item_<submission_id>`，`created_at` 取提交时刻；提交时刻随准入记录
   落账（`occurred_at`）。运行时应用与接管恢复由同一个函数构造它，得到的逐字相同。
4. **应用的落账与取消无关，投影可以被取消**。此前应用只有投影一步，可以整体被取消；现在多了一次写入，
   写到一半被截断会让写者进入结果未知状态。raw cancel 延迟到写完再抛。
5. **接管时应用未应用的消息**。进程死在两个批次之间——消息还在排队，或者刚准入还没轮到——时，
   Journal 里有准入没有 applied。准入是 durable 承诺：接管把它们按准入顺序应用，落在恢复批次末尾。
   它们的 turn 不补跑；消息在对话里，模型在下一个 turn 看到。
6. **token 的校验随之收窄**。actor 拿到的 token 只带准入记录；它在应用前重新校验 hash 链、identity
   与提交时刻，被篡改的 token 冻结 Session、不产生对话项。

## 考虑过的其他做法

- **保留原子三记录，应用时补一条「重新排位」的对话项**。Journal 里同一条消息出现两次，读 Journal 的人
  都要知道「取最后一次」；崩溃时还要靠「后面有没有更早的 submission 的内容」来推断它有没有被应用过。
- **保留原子三记录，重建与投影改用「应用顺序」**。应用顺序得另有记录承载，投影器的序号单调性检查
  也要改；改动落在 Journal core 这一层，影响面比改准入大。
- **准入等到 gate 空闲再落账**。`submit()` 返回时消息还没有 durable，进程一死就丢。

## 影响

- R1：无业务概念。
- R2：无影响。
- R3：事件不变。
- R4：应用的落账不可被取消截断；排队、等待 gate 仍可取消。
- R5：接管后的 history 与进程死之前模型看到的一致；已准入未应用的消息不丢。

### 行为变化

- 不排队时记录的先后与此前相同（准入、对话项、applied 相邻），只是分属两个批次。
- 排队时对话项与 applied 出现在它前面那个 turn 的记录之后。
- 已准入、尚未应用的消息在接管时不再使 resume 被拒。
- 旧格式的 Journal（对话项在准入批次里）照常可读、可接管。

### 已知边界

- 审计模式下 `CancelTurn` 指向还在排队的 submission 得到 `not_found`（既有行为）：排队的消息取消不了。
- 释放 Session 时 root turn 正在进行的工具调用没有结果，Session 却以 `complete` 终结（既有缺口，另行处理）。

## 验证

`tests/loop/test_audit_queued_submission.py`（3 项）：排队的消息在它的 turn 开始时进入对话，四种顺序一致、
投影健康；释放时仍在排队的消息只应用不运行；崩溃前已准入的消息在接管时应用。按新契约更新了
`test_audit_submission_admission.py`、`test_audit_submission_admission_hardening.py`、
`test_audit_submission_recovery.py` 里依赖原子三记录的断言。全量 `pytest tests/` 通过
（3790 passed, 17 skipped）；ruff 门禁与 `mypy src/` 清零。真实 LLM 台账随集成一并刷新。
