# ADR 0053：审计 Session resume 与跨进程写者接管

- 状态：Accepted
- 日期：2026-09-28
- 关联：Amends ADR 0025（SessionJournal 作为 Session 真相源）；契约
  [session-journal-core](../architecture/capabilities/session-journal-core.md) §5.1–5.2 / §8、
  [session-journal-business-integration](../architecture/capabilities/session-journal-business-integration.md) §13

## 背景

ADR 0025 把 strict audit Session 的事实源定为 SessionJournal，但落地到 Phase 1 时留了两个缺口：

1. **writer fencing 只在进程内**。`JsonlSessionJournalCore` 用实例内 `_writers` 字典做 lease 校验；另一个进程
   （或同进程另一个 core 实例）对同一 Session 文件追加时，只能靠每次 append 前重扫文件核对 tail「事后发现」。
   扫描与写入之间没有互斥，两个 writer 可以在同一窗口各写一个 batch，hash chain 随之分叉。契约原文是
   「跨进程接管与更高 epoch 由 Phase 2 负责」。
2. **审计 Session 无法 resume**。`EnginePool.get_or_create(resume_thread_id=...)` 在 audit 模式下静态拒绝
   （`audit_resume_unsupported`）。进程一崩，该 Session 只能废弃，尽管恢复所需的一切都已 durable 在 Journal 里。

## 决策

### 1. 用 OS 级 flock 做真互斥，而不是乐观检测

`create_session` / `open_existing` 在任何 mutation 前获取 `<session>.journal.lock` 的
`flock(LOCK_EX | LOCK_NB)`，fd 一直持有到 `close_session` / `close`。

- **为什么不是继续「append 前重扫 + CAS」**：乐观检测只能在冲突发生后发现，而 Journal 的追加一旦写出就是
  durable 事实，发现时两个 writer 的 batch 都已落盘，只能冻结，无法回滚。互斥必须在写之前。
- **为什么是 flock 而不是 lease 文件 / 心跳**：进程崩溃时内核自动释放 flock，不需要过期时间、时钟或清理
  流程；lease 文件要么靠 TTL（时钟漂移下不安全），要么崩溃后需人工删除。flock 以「打开的文件描述」为单位，
  同进程两个 core 实例也互斥，测试与多进程部署语义一致。
- **锁文件永不删除**：释放时删除会让「删除前已 open 旧 inode」与「删除后 open 新 inode」的两方同时持锁。
- **非 POSIX 显式失败**（`JournalLockUnsupportedError`）：静默不加锁等于回到没有互斥的状态，比报错更糟。
- 锁经可注入 `WriterLockAdapter`：与 `SyncFileAdapter` 同风格，便于测试与替换（如网络文件系统需要的其他锁）。
- 提交结果未知时锁 park 到 `close()`：后台线程可能仍在写，此时放锁会让另一进程读到半截 batch 后接管。

flock 是建议锁，只约束走 core 的 writer；绕过 core 直接写文件不在防护范围内，由 strict verify 兜底发现。

### 2. 接管 = epoch+1 + 一条 `writer_takeover` 记录；verify 强制 epoch 单调

`open_existing` 持锁后 strict scan，以 `tail.writer_epoch + 1` 追加 `writer_takeover`（payload 含 previous epoch、
接管前 tail seq / hash、writer id、operation id）。strict verify 新增：Session 起点 epoch 必须为 1；batch 内
epoch 恒定；epoch 只能由 batch 首条 `writer_takeover` 单步抬升且 lineage 指向接管前 tail；递减一律
`JournalIntegrityError`。

这样 epoch 变化本身成为 hash chain 里可审计的事实，而不只是 envelope 上一个无人校验的整数；「未授权的新
writer」和「旧 writer 复活后回写」都会在 verify 时暴露。`writer_takeover` payload 放在 core `models.py`
（与初始化三记录同属 core 自写记录），不进入业务 `records.py`。

同 operation 的重试：同实例返回原结果；接管记录已 durable 但调用方丢了 ack 时，只有该记录仍是 tail 且属于同一
writer 才复用其 epoch；其后已有写入说明该 epoch 已被使用过，重试必须冲突，否则两个 lease 会共享一个 epoch。

### 3. 已写 `session_ended` 的 Session 不可重开

`session_ended` 是 ADR 0025 定义的唯一 durable 终态，resume 之后再追加记录会让「终态之后还有事实」，审计
语义自相矛盾。core 层 `open_existing` 直接拒绝（`JournalSessionEndedError`），业务层映射为
`audit_resume_session_ended`。需要继续对话的业务方开新 Session。

### 4. 未结算 effect 一律 fail closed，不自动续跑

ADR 0025：「未匹配 intent 在恢复时一律是 UNKNOWN」。resume 扫描 committed records，下列任一存在即拒绝并列出
record id（`audit_resume_recovery_required`）：LLM attempt 无 checkpoint、tool intent 无 outcome、skill 选择
无 finished、submission accepted 无 applied、或任一终态已 durable 为 `unknown`。

- 自动把它们补成 `unknown` 再续跑，会让模型在「外部写操作也许发生过」的前提下继续推理——对非幂等 effect
  这正是审计模式要防止的情况。是否重试、补偿还是放弃，只能由运维 / 业务按 effect 的 reconciliation 策略裁决；
  内核不替人猜。
- resume 先做只读预检，注定被拒的请求不写接管记录，避免反复尝试无谓抬高 epoch；持锁后再权威重读，覆盖预检
  与接管之间的窗口。
- resume 通过后 Engine 构造 / warmup 失败只释放 lease，不写 `session_ended`：这是本次恢复的失败，不是 Session
  的终结，之后可以再次接管。

通过检查后，用 root thread 已提交的 `conversation_item` 重建 history，coordinator 使用新 lease，projector 复用
既有投影 thread 并以 Journal 为真相核对（前缀补齐，分叉只标 stale），audited turn index 接续 Journal。

## 未做 / 边界

- 未结算 effect 的 repair / reconcile / unfreeze 状态机与 recovery lease（ADR 0025 Phase 2 其余部分）：本 ADR 只
  提供「安全续跑」与「明确拒绝」两个出口，不提供把 UNKNOWN 改判为已知结局的写入路径。
- 投影领先 / 分叉时只标 stale，不自动删除重建；运维删除投影文件后，下次 resume 会被 marker 缺失拒绝，需另行
  重建投影 metadata（不在本 ADR 范围）。
- 跨 resume 重试同一 LLM operation（retry generation）不支持；续跑的新 turn 使用新 submission id，operation
  identity 天然不同。

## 后果

- `JournalBusyError.writer_id` 变为 `str | None`（`None` 表示持有者是其他进程 / 实例）。
- `EnginePool.get_or_create` 在 audit 模式下的 resume 从静态拒绝变为受控接管；`AuditCapabilityError
  ("audit_resume_unsupported")` 与 `validate_audit_session_request` 删除，改为 `AuditResumeError(code)`。
- 同一 core root 下多了 `<session>.journal.lock` 文件。
