# ADR 0099：审计模式放开 join-barrier

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §19](../architecture/capabilities/session-journal-business-integration.md)；
  [detached-spawn](../architecture/capabilities/detached-spawn.md)；ADR 0015、0025、0098

## 背景

ADR 0098 放开了分离式派发，把 join-barrier 留了下来。只有派发没有 barrier，「并行铺开几个子任务、
全部跑完后汇总」这个最常见的用法在审计模式下要靠模型自己轮询再汇总。

与 0098 的缺口相同：`engine.set_join_barrier()` 是公共 API，审计 Session 里直接调用它，聚合 turn
照常运行却不进 Journal。

实现中发现一处与模式无关的顺序问题：句柄的内存状态先变成终态，终态才开始持久化。这段时间里查询
句柄的人已经能读到结果，而结果还没有写下。

命中 ADR 0017 规则①。

## 决策

1. **三条记录**：`barrier_registered`（登记）、`barrier_fired`（点火，与聚合 thread 的创建、绑定、
   种子同批）、`barrier_settled`（聚合 turn 结束，与聚合 thread 的 `thread_terminal` 同批）。
   共用一个 operation：barrier id。
2. **`barrier_fired` 记下点火时各成员的终态与交给聚合 skill 的输入**。聚合 skill 看到什么、
   依据的是成员的哪个结局，要能从记录里直接读出，不用再去翻各成员的终态记录拼。
3. **聚合 turn 的终态要落账**。非审计模式不记它——聚合 turn 不登记为句柄，结束了就结束了。
   审计模式下聚合 thread 是一个有始有终的 thread，没有终态就分不清它是跑完了还是进程死了。
4. **不写锚点条目**，理由同 ADR 0098 决策 3。
5. **接管时 barrier 表由记录重建**，有 `barrier_fired` 的记为已点火。
6. **登记了没点火的 barrier 在接管后照常点火**。它等的成员在接管时已全部有终态（没跑完的被落为
   `cancelled`），条件已经满足。聚合 skill 看到的是真实情况：哪些成员跑完了，哪些没有。
7. **已点火、聚合 turn 被中断的 barrier 落 `cancelled`，不再点火**。理由同 ADR 0098 决策 5；
   另外「至多点火一次」是 barrier 的既有保证。
8. **登记记录 ack 之后才进 barrier 表**。此前是先进表再持久化；持久化失败时表里留着一个没有
   记录的 barrier。两种模式一并改正。
9. **终态的顺序改为「持久化 → 句柄状态 → 事件」**，两种模式一并改正。持久化期间该句柄记为
   正在收敛，其他收敛路径（kill、失败兜底）见到即让开，「终态恰好一次」的保证不变。

## 不做

- **任一结束触发、超时触发**：非审计模式也没有。
- **把聚合 turn 登记为句柄**：会改变非审计模式的可见行为（`join_skill` 能查到聚合结果），不属于本次范围。

## 影响

- R1：无业务概念。
- R2：不触发压缩。
- R3：事件不变。
- R4：聚合 turn 的取消 token 不变；释放 Session 时被取消并落终态。
- R5：接管后 barrier 表与 Journal 一致；没点火的会点火，已点火的不重复。

### 行为变化

- 审计 Session 里的 `engine.set_join_barrier()` 现在落账，聚合 turn 带审计状态。
- 审计模式的静态门放行 `await_skills`。
- 两种模式：句柄状态在终态持久化之后才变为终态；barrier 在登记持久化之后才进表。
  终态事件的时机与内容不变。

## 验证

`tests/loop/test_audit_spawn.py` 新增 6 项 barrier 场景（共 18 项）。全量 `pytest tests/` 通过
（3781 passed, 17 skipped）；ruff 门禁与 `mypy src/` 清零。真实 LLM 台账随集成一并刷新。
