# ADR 0098：审计模式放开分离式派发

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §18](../architecture/capabilities/session-journal-business-integration.md)；
  [detached-spawn](../architecture/capabilities/detached-spawn.md)；ADR 0015、0025、0053、0070、0076、0097

## 背景

审计模式的静态门拒绝 `spawn_skill` 等工具，但 `engine.spawn_skill()` 这个公共 API 没有门：
在审计 Session 里直接调用它，子 skill 照常运行，整个过程不进 Journal——子 thread 由 store 直接创建，
子 runner 不带审计状态，它的 LLM 调用与工具调用都没有记录。这是一个审计缺口。

ADR 0025 约定「Spawn：先提交 child intent，再创建 child，结果关联 parent intent」，此前未实现。

命中 ADR 0017 规则①。

## 决策

1. **发起是一个原子批次**：`spawn_started` + 子 thread 的 `thread_created` / `thread_bound` + 种子对话项。
   批次 ack 之后子 skill 才开始运行。与同步 `call_skill` 的发起批次同构。
2. **终态是 `spawn_settled` + 子 thread 的 `thread_terminal`**，先于终态事件。
3. **不写锚点条目**。非审计模式靠父 thread 的 `spawn` 锚与子 thread 的 `spawn_settled` 锚做冷恢复；
   审计模式下这些事实已经在记录里。更重要的是写者问题：子 skill 结束的时刻不可控，那时父 thread 上
   可能正有 turn 在写。两个写者各自「追加记录 → 投影」，投影的顺序就可能与 Journal 的顺序相反，
   投影被判为 `sequence_regression`。不往别的写者的 thread 上写，这个问题就不存在。
4. **句柄表由记录重建**。接管时读 `spawn_started` / `spawn_settled`，不读对话项、不读投影。
   投影是可以落后的派生物，凭它推断句柄状态会得到「停在 running、却没有任何任务在跑」的句柄。
5. **接管时没有终态的派发落 `cancelled`，不续跑**。接管的进程没有它的运行态；从头重跑会把子 skill
   已经做过的事再做一遍。子 thread 上的工具调用先按既有规则结算（与被中断的同步派发一样），
   再落派发的终态，同属一个恢复批次、全有或全无。发起方此后查询得到 `cancelled`，由它决定要不要重派。
6. **子 thread 上的调用不能停下等人**，在工具层就判为能力违约（记 error 终态后冻结），不等 turn 以
   suspended 结束。`Resume` 只认 root thread；让子 thread 写下一条没有人能结清的 `turn_suspended`
   没有意义。同步 `call_skill` 的子 skill 同样处理——此前它要到父 runner 收到 suspended 结局时才冻结，
   期间子 thread 已经写下了挂起记录。
7. **operation 取句柄 id**，不挂在发起它的工具调用之下。`engine.spawn_skill()` 可以在任何 turn 之外被
   调用，这时没有工具调用可挂。
8. **等待工具的时长设上限**（收敛期限的一半）。审计模式下一次工具调用必须在收敛期限内给出结果，
   否则 Session 冻结；模型给 `wait_peer` 传一个较大的超时不应该有这样的后果。到点返回 `timeout`
   是这两个工具本来就有的正常结果。
9. **持久化从 `SpawnDriver` 里分出来**（`spawn_ledger`）。句柄表、取消、K1、事件不关心事实落在哪里；
   两种模式只在「发起」「锚点」「终态」三处不同。

## 不做

- **join-barrier**：聚合 turn 由最后一个子 skill 的收尾触发，登记与点火都要落账，另行立项。
- **peer 消息**：消息要进目标 thread 的对话，而目标 thread 有自己的写者，落账时机需要单独设计。
- **子 thread 上的挂起与错峰 Resume**：需要 `Resume` 能指向子 thread，以及挂起态派发的接管语义。
- **被唤醒重跑、子 thread 的 rewind**：同一个句柄多次运行，终态记录要带轮次。
- **后台 shell 任务**（`run_in_background` / `wait_for_task`）：与 skill 派发无关，仍被静态门拒绝。

## 影响

- R1：无业务概念。
- R2：不触发压缩；子 thread 有独立的 cache 生命周期（既有行为）。
- R3：事件不变。
- R4：每个派发的取消 token 不变；释放 Session 时仍在运行的派发被取消并落终态。
- R5：接管后句柄表与 Journal 一致；没有「停在 running」的句柄。

### 行为变化

- 审计 Session 里的 `engine.spawn_skill()` 现在落账，子 runner 带审计状态。
- 审计模式的静态门放行 `spawn_skill` / `kill_skill` / `join_skill` / `wait_peer`。
- 新的拒绝分类 `arguments_not_canonical`（仅审计模式出现）。
- 同步 `call_skill` 的子 skill 停下等人时，冻结发生在那次工具调用结算时，子 thread 不再留下
  `turn_suspended`。
- 非审计模式无变化（持久化代码搬到 `spawn_ledger`，行为逐字保留）。

### 已知边界

- 子 thread 上未结算的 LLM 请求仍使 resume 被拒（与 root thread 相同）。
- 发起它的工具调用与这次派发经 outcome 里的 `handle_id` 关联。

## 验证

`tests/loop/test_audit_spawn.py`（11 项）。更新了 `test_audit_config.py` 与
`test_audit_shutdown_lifecycle_review.py` 里依赖旧行为的断言。全量 `pytest tests/` 通过；
ruff 门禁与 `mypy src/` 清零。真实 LLM 台账随集成一并刷新。
