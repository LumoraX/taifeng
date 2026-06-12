# ADR 0018: Rewind 的 thread 寻址 —— spawn 子 thread 的截断重推

- 状态：Accepted
- 日期：2026-06-12
- 关系：扩展 ADR 0014（turn-rewind 热场景）/ ADR 0016（冷场景重建,不推翻其截点 / node_id / 重放语义）；与 ADR 0015（detached-skill-spawn）协作（复用其句柄 / 收敛点 / 子 runner 重建机制）；立项依据 ADR 0017 规则①（内核机制缺口）

## 背景

`Rewind` Op 此前是 **root-thread 作用域**：只带 `node_id` + `mode`，`_handle_rewind` 全程作用于根 `self._history`。detached spawn 出去的子 thread 各有独立 history（store 持久、引擎不常驻内存），root 的 Rewind 够不到。

对照：`Resume` 早已支持 thread 寻址（`thread_id` → `match_suspended_spawn` → `resume_spawn`，ADR 0015）。**Rewind 缺对称能力**——这是内核机制缺口（ADR 0017 规则①），不是产品功能：

- **挂起态**的失败 retry 已由 `SuspendByDefaultPolicy` + Resume 覆盖（本提案曾因此于 2026-06-10 标作废）；
- 但**非挂起态**的失败重试（spawn 已落 `error` 终态 / 进程中断遗留态）仍无解——业务侧"长程任务某一步失败后，从失败步人工 retry（保留前序步骤 + 已答 HITL）"只能降级为整任务重 spawn。

## 决策

### 决策一：`Rewind` 增可选 `thread_id`，按 thread 分流（对称 Resume）

`Rewind.thread_id: str | None = None`。缺省 / 指根 → 既有根路径**零变更**；指向 spawn 句柄的 `child_thread_id` → 路由 `SpawnDriver.rewind_spawn`（新模块 `loop/spawn_rewind.py`，driver 协作器范式：无自有状态，运行态单一持有在 driver）。

**为什么只认 spawn 句柄**：call_skill 阻塞子链的中间层 thread 生命周期附属父 turn、无独立句柄与重推驱动；spawn 子 thread 是独立根 turn，有完整的「重建 runner → finalize 收敛」机制可复用。v1 边界明确。

### 决策二：活性守卫，禁状态白名单

拒绝判定按**活性**而非句柄状态：

| 拒绝 | reason |
| --- | --- |
| thread 不属于任何 spawn 句柄 | `unknown_thread` |
| 子 runner 热跑中 / rewind 在飞 | `thread_running` |
| 子 thread 有活跃挂起 | `turn_suspended`（挂起走 Resume,职责不重叠） |

**为什么不按状态白名单（如"仅 error/done 可 rewind"）**：冷重建的状态推断（`_infer_spawn_status_from_child`）只产出 suspended / done / running，**永不产出 error**——失败子 thread 冷启后呈现为 done 或 running。按状态拦会把业务最核心的冷重试场景挡死。放行集合（error / done / cancelled / 中断遗留 running）全部是「无并发写者」的安全态。

### 决策三：子 thread 节点表按需推导，截断 = 落 marker

- 节点表：`engine.rewind_nodes_for(thread_id)` —— raw → `reconstruct_logical_history` → `derive_rewind_log`。**必须先 reconstruct**：`_load_thread_items` 是 raw store 项，含被折叠 / 被截断的废弃项，直接 derive 坐标错位。checkpoint 下标与 marker `cut_index` 在 reconstruct 顺序重放下同坐标系，多次 rewind 叠加自洽（与 ADR 0016 同一论证）。
- 截断：子 thread 引擎不常驻、无内存 `_history` 可截 → 截断动作 = `[rewind]` marker（`cut_index`）append 到子 thread store（append-only，R5），重推时 reconstruct 自然得到截断后历史。
- 重推：复用 `_resume_spawn_settled` 尾段范式（句柄回 running → `_build_child_runner` → live 登记 → `_finalize_spawn` 单点收敛）。**不是** `resume_spawn_nested`（那是 CHILD_SKILL 挂起的下探回填链，与 rewind 无关——设计验证时修正了 proposal 原稿的这处设想）。

### 决策四：不自动重聚合

rewind 已 done 且 barrier 已 fired 的 spawn：重推得新结果，但聚合 turn 不二次触发（`_fired_barriers` 幂等守卫）。要重聚合业务自行再 `set_join_barrier`——避免聚合 turn 的重复副作用（外发 / 落库等）由内核隐式触发。

## 后果

- 失败 spawn 的人工 retry 从"整任务重 spawn（丢前序步骤与已答 HITL）"提升为"从失败步续跑"。
- `RewindRejected.reason` 新增 `unknown_thread` / `thread_running` 两个取值；`TurnRewound.data` 在子 thread 路径携带 `thread_id`（R3）。
- 并发窗口：Rewind 与 Resume 对同一句柄的竞争由各自在飞集 + 活性守卫闭合，不引入跨 Op 全局锁（代价不成比例,v1 接受）。
- 多实例部署下"中断遗留 running"的活性不可见（live 表单实例内闭合）——多实例互斥是业务侧部署约束，记入能力契约边界。

## 验证

- mock 全场景：`tests/loop/test_rewind_thread.py`（16 用例:字段 / 节点表 / 五守卫 / re_reason / retry_tool / 再失败叠加 / stale running 放行 / 冷重放一致 / kill / barrier 幂等）；全量 1065 passed。
- 真实 LLM：`examples/real_llm/capability_matrix.py` 全量回归（台账 `docs/real-llm-ledger.md`）。
