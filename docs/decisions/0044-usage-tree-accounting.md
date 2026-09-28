# ADR 0044：会话用量整棵 turn 树共享记账，采样即入账

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/usage-tree-accounting.md](../architecture/capabilities/usage-tree-accounting.md)；[turn-resource-guards](../architecture/capabilities/turn-resource-guards.md)；kernel-gap-analysis K2

## 背景

2026-09-28 内核 review 核实：`engine_runner.py` 在根 turn 收尾时执行
`_session_tokens += runner.total_usage.total_tokens`，这是会话累计**唯一**的入账点。
`call_skill` 子 turn、detached spawn、子 thread 续跑链的 usage 从不回灌；子 runner 构造时只拿
`session_tokens_used` 基线做 K2 检查，兄弟子树互相不可见。K2 `max_session_tokens`（OOM-killer）
因此可被子树整体绕过——恰恰是最容易失控的多 agent 扇出场景失守。`child_resume_chain` 的注释也已
自认「续跑用量不回写 engine 计量为既有缺口」。另外 `EventMsg` / `turn_completed` 没有 thread_id /
skill_id，无法按 skill 归因成本。

## 决策

1. **一本账**：engine 持有 `SessionUsageMeter`，注入整棵 turn 树的每个 runner；`_session_tokens`
   变为其总量视图（保留 setter 兼容白盒测试与宿主设定基线）。
2. **采样即入账**：在 `accumulate_usage` 处实时入账，而非 turn 收尾。已花掉的 token 不因 turn
   失败 / 取消 / 冻结而不计；并发兄弟子树立即彼此可见。
3. **不改 `usage` 语义，另加 `subtree_usage`**：`turn_completed.usage` 仍是 runner 自身用量——
   已有消费者按事件求和不会重复计数。整棵阻塞子树的总量由新字段 `subtree_usage` 给出。
4. **detached spawn 不进父 subtree**：spawn 的生命周期可长于父 turn，归并到父会让父的
   `turn_completed` 数字取决于调度时序；它的用量走会话账与其自身 `turn_completed`。
5. **归因维度只用 thread_id / skill_id**（R1）；成本换算（单价）留 userspace。

## 备选与否决

- **子 runner 收尾时把 usage 回加父 `total_usage`**：否。改变既有 `usage` 语义导致按事件求和的
  消费者重复计数；且回加发生在子 turn 结束时，并发兄弟在子 turn 运行期间仍不可见。
- **给 EventMsg 外壳加 thread_id / skill_id**：暂否。外壳是全局协议，所有事件都要填，改动面大；
  归因需求集中在用量，放在 `turn_completed` 与 `introspect()` 即可满足。
