# ADR 0035：spawn 二次驱动单入口，子 thread 终态持久锚，续跑链取消即停

- 状态：Accepted
- 日期：2026-09-06
- Amends：ADR 0015（detached spawn 的 kill / 冷恢复 / K1 语义）、ADR 0018（thread 寻址 rewind 的重推路径）
- 关联：[detached-spawn 契约](../architecture/capabilities/detached-spawn.md)；[suspend-resume 契约](../architecture/capabilities/suspend-resume.md) §子 thread resume 续跑链；[agent-loop 活文档](../architecture/agent-loop.md) §分离式 spawn；openspec change `wave2b-spawn-resume-truth`

## 背景

2026-09-03 全系统审查把 detached spawn 的**二次驱动路径**列为 Wave 2b。ADR 0029 已把根 turn 收敛为「单写者 + 热 == 冷由构造保证」，但子 thread 这一侧仍是五份手写拷贝，且读的是 store 原始转录。用 SimClient 逐条核对后确认五个真实缺陷（回归用例 `tests/loop/test_spawn_redrive_truth.py`，改动前全部 FAIL）：

1. **冷推断读 raw**：`_load_thread_items` 直接返回 `store.load_thread`，只有 rewind 链自己 reconstruct。resume / 嵌套 resume / peer 唤醒 / `_run_thread_turn` 把含 `[rewind]` marker 与被截断旧圈（或 compacted 占位 + 被替换原文）的 raw 当 `history_buffer`——LLM 看到被 rewind 掉的旧派发和中段 `role=system` 的 marker；`_infer_spawn_status_from_child` 据废弃 assistant 文本判 done。
2. **冷重武装早于句柄重建**：`run()` 首行重武装 TTL 时 `suspended_handles()` 尚空（rebuild 要等 `_root_cancel` 就绪），挂起态 spawn 子 thread 的 TTL 冷恢复后永不武装、过期的永不裁决。
3. **五份驱动拷贝各自为政**：resume / rewind 重推不占 K1 slot（`max_concurrent_spawns` 被绕过）；`set_result(running)` 与 token 登记之间隔着 await，kill 打在窗口里要么取消旧 token、要么被 running 覆盖终态——同一句柄先 `spawn_cancelled` 后 `spawn_completed`，runner 跑在已 kill 的句柄上；重载历史到登记 live runner 之间 peer 投递落 store 不进 buffer。
4. **kill 挂起句柄只改内存**：子 thread 挂起记录仍活跃 → 冷恢复复活为 suspended、TTL 对已 kill 句柄提交裁决、可再次 Resume；done / error 同样无持久锚，冷推断只能猜。
5. **续跑链取消不停链**：leaf 被 `CancelTurn` 后链把 `sub_skill_failed: cancelled` 当输出继续重跑父层和根（R4 违约）；若只停不写，根仍挂在 CHILD_SKILL 上，新消息 / Resume / Rewind 全被拒，会话卡死。

## 决策

### 1. 子 thread 逻辑 history 单一入口

`engine._load_thread_items(tid)` 返回 `reconstruct_logical_history(load_thread(tid))`。所有非根 thread 的重载 / 推断 / 路由 / TTL 活跃性验证都经它；`reconstruct_logical_history` 对逻辑 history 是恒等映射，二次 reconstruct 冗余删除。spawn 子 thread 的 TTL 冷重武装移到 `_rebuild_spawn_state_from_history` 收尾（句柄表就绪之后），`_arm_ttl_timer` 按 record_id 去重。

### 2. `SpawnDriver._drive` 是 detached 子 runner 的唯一构造点

五条路径只提供 `prepare`（在线程锁内产出 `SpawnDrivePlan`：逻辑 history + 采样参数）与期望状态；入口内闭合：K1（未持有则等待式预留——续跑不是新的发起，满额抛错会把合法核销变成 error 终态，故排队；句柄终态 / 根取消即放弃）→ 线程锁内 prepare → **状态 CAS**（resume 期望 suspended；被 kill 则放弃，kill 胜出）→ **同一同步步**登记 token、回写 running、构造 runner、登记 live → run → `_finalize_spawn`；宽 except → `_settle_failed`；finally 释放 slot。`spawn_skill` / rewind / peer 唤醒各自在同步步派生并登记 token，runner 起跑前的 kill 也能命中。按 child_thread_id 的锁使 peer 投递的「非 live 判定 → 落史 / 唤醒」段与重载段互斥。

### 3. 终态落子 thread 持久锚；suspended-kill 落 resolved marker

新 kind `spawn_settled{handle_id, status, result}`，由 `_finalize_spawn` 终态分支 / `_settle_failed` / suspended-kill 统一落到**子 thread**（不进 LLM 视图，随 rewind 截断折叠）。为何不落父 thread：父 thread 在飞 turn 期间只有 runner 一个写者（ADR 0029），detached 收敛时刻不可控；子 thread 此时无热 buffer，store 即真相。suspended-kill 改经 `_settle_cancelled_suspended`：同步步置 cancelled → 落 `suspend_resolved:<record_id>` + `spawn_settled(cancelled)` → 撤销该 record 的 TTL → emit → barrier 重查。冷推断以逻辑 history 上「最后活跃 suspension」与「最后 spawn_settled」靠后者为准，两者皆无退回旧启发（旧转录前向兼容）。

### 4. 续跑链取消即停并解链

链级 token 一次派生（`root_cancel.child("resume:<sub_id>")`），各层 turn token 派生自它；`_run_thread_turn` 遮蔽 / 还原链级 `_pending` 登记项，层间的 `CancelTurn` 同样可达。任一层 outcome `cancelled` → 返回哨兵 `CHAIN_CANCELLED_RESULT`（= `sub_skill_failed: cancelled`），上层照常回填 fco + record 级结算（partial 照旧）但**不重跑**；根链以 `turn_failed{kind: cancelled, is_root: true}` 终结 Resume submission（根此刻无活跃挂起，可接新 UserMessage / Rewind）；spawn 嵌套链解到 spawn 子 thread 后句柄经 `_settle_cancelled_suspended` 收敛为 cancelled（可 Rewind 重推）。

## 替代方案

- **只在五个重载点各自 reconstruct**：又是五份拷贝，冷推断 / TTL / 路由继续读 raw；否决。
- **终态锚落父 thread**（与 `spawn` 锚同处）：detached 收敛时刻不可控，父 thread 在飞时会成为第二个写者，重蹈 2a 的整表覆盖；否决。
- **resume 重推满额时抛 `SpawnLimitError`**：把一次合法的 HITL 核销变成 error 终态，TTL 哨兵 Resume 也会被拒后失去定时器；否决，改排队。
- **链取消只停不解链**：根仍挂在 CHILD_SKILL 上，会话卡死；否决。**解链后重跑父层**：用户已喊停，上层继续采样是 R4 违约；否决。

## 后果

- **BREAKING（行为）**：resume / rewind 重推现在占用 `max_concurrent_spawns`，满额时排队而非立即跑；宿主可调大上限或接受排队。
- 新 ResponseItem kind `spawn_settled`；子 thread 转录多两类记账项（settled 锚、suspended-kill 的 resolved marker），均不进 LLM 视图。
- `spawn_handle.py` 新增 `SpawnDrivePlan`；`SpawnSlotRegistry.acquire_manual`；`SpawnDriver` 新增线程锁表。
- mid-flight 中断 spawn（冷推断 running）仍不自动重驱动；barrier 聚合 turn 仍不登记为句柄——两者留 v1 边界。
