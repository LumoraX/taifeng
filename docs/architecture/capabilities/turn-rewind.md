# Capability: turn-rewind

## Purpose

把一次 root turn 的执行轨迹拆成一张**可寻址的回访节点表**，业务侧可对**任意节点直接 retry**：既能重跑某一次 LLM loop 采样（iteration 节点），也能重跑某一次工具 / `call_skill` 派发（dispatch 节点）。重试发生在**同一次 turn 内**；子 skill 全程 `entry: false`，**绕开 entry/call_skill 互斥**（不依赖把子 skill 变 entry，也不放松 [`skill-dispatch`](skill-dispatch.md) 的 `cannot_call_entry_skill` 约束）。

决策记录：[ADR 0014](../../decisions/0014-turn-rewind.md)（热场景 v1）、[ADR 0016](../../decisions/0016-cold-rewind-rebuild.md)（冷场景重建）
实现：`src/taifeng/loop/rewind.py`（节点结构 + `derive_rewind_log`）、`src/taifeng/conversation/reconstruct.py`（`reconstruct_logical_history`）、`loop/turn.py`（记录 + retry_tool 补跑）、`loop/engine.py`（`_handle_rewind` + `rewind_nodes()` / `rewind_nodes_for()` + 冷加载接线）、`loop/spawn_rewind.py`（spawn 子 thread 寻址链：守卫 / 截断 / 重推）。
ADR：[0014](../../../docs/decisions/0014-turn-rewind.md)（热场景）、[0016](../../../docs/decisions/0016-cold-rewind-rebuild.md)（冷场景重建，`Supersedes #0014` 的 node_id 段）。

## 数据契约

### `RewindCheckpoint`（`loop/rewind.py`）
一个 turn 内可回退锚点，**只记 history 下标**（append-only 不破，R5）：

| 字段 | 含义 |
| --- | --- |
| `node_id` | turn 限定稳定唯一 id（`t{k}:it{n}` / `t{k}:disp{m}`，`k` = 累积 user_message 数，1-based） |
| `turn_index` | 所属 turn 的 k 值（与 node_id 中的 `t{k}` 一致），供按 turn 过滤 |
| `kind` | `iteration` \| `dispatch`（`turn_root` 收敛进首个 iteration 节点 `t{k}:it1`） |
| `history_len` | re_reason 截点 = 该 history 长度 |
| `cache_anchor` | 回退时还原的 `cache_anchor_index` |
| `iteration_index` | 所属采样圈 |
| `call_id` / `target_id` | 仅 dispatch：被派发的 call_id / 工具名 |
| `inner_history_len` | 仅 dispatch：retry_tool 切点（`function_call` 后 / `function_call_output` 前） |
| `args_digest` | 仅 dispatch：原始 args 摘要（供 UI / 审计） |

### `Rewind` Op（`loop/submission.py`）
`{ node_id, mode ∈ {re_reason, retry_tool}, new_args?, thread_id? }`。`new_args` 仅 `retry_tool` + dispatch 节点有意义。`thread_id` 缺省 None = root thread（向后兼容，既有语义零变更）；指向某 detached spawn 句柄的 `child_thread_id` 时走子 thread rewind 路径（见下「thread 寻址」Requirement）。

## Requirements

### Requirement: root turn 记录完整回访节点表

root TurnRunner SHALL 在每圈 `_sample_once` 采样前记一个 `iteration` 节点，并在每次工具 / `call_skill` 派发（`function_call` 追加处）记一个 `dispatch` 节点。每记一个节点 SHALL emit `rewind_checkpoint_recorded`（R3）。**仅 root turn**记录；call_skill 阻塞子 turn 节点 v1 不入表（detached spawn 子 thread 的节点表不走 live 记录，按需经 `rewind_nodes_for` 从 store 推导，见「thread 寻址」Requirement）。

节点表的产生是通过 `derive_rewind_log(history)` 纯函数统一推导（`loop/rewind.py`），为**冷加载 / 热 turn 结束 / CompactNow 三处唯一产出方**。热 turn 执行中的 live `RewindLog` 仅用于 emit `rewind_checkpoint_recorded` 事件（R3），turn 结束后以 `derive_rewind_log` 重算覆写。节点表回写 engine，`engine.rewind_nodes()` 只读暴露。

node_id 格式：`t{k}:it{n}`（iteration）/ `t{k}:disp{m}`（dispatch），其中 `k` = 到本 turn 为止的累积 `user_message` 数（1-based），由 `count_turns(history)` helper 从逻辑 history 算出，**不依赖 engine._turn_index**（后者冷加载不回填）。

#### Scenario: 自治链跑 N 圈 M 次派发
- **WHEN** 一次自治 turn 跑 3 圈、前 2 圈各 1 次 `read_skill` 派发
- **THEN** `rewind_nodes()` SHALL 含 3 个 iteration + 2 个 dispatch 节点
- **AND** 每个 dispatch 节点 `history_len` == 所属 iteration 节点的 `history_len`（re_reason 切点归一，因 assistant 消息原子、不可切在并行 tool_call 中间）
- **AND** `inner_history_len` > `history_len`（retry_tool 切点在 fc 之后）

### Requirement: 冷场景重建——跨进程 / 冷 worker 也能 rewind

引擎冷加载（传入 `initial_history`）时，SHALL 把 raw transcript 重建为与热内存等价的逻辑 history，再在其上推导全 turn 节点表；使历史任意 turn 的节点均可寻址 rewind，行为与热场景一致。

**重建步骤（均在 engine `__init__`）**：
1. `reconstruct_logical_history(raw_initial_history)` → 逻辑 history（`conversation/reconstruct.py`）
2. `derive_rewind_log(logical_history)` → 重建完整节点表（`loop/rewind.py`）

重建完成 SHALL emit `RewindTableRebuilt{thread_id, turn_count, node_count}`（R3），由 `EnginePool` 在 spawn-state 重建路径触发。

**依赖契约（R5）**：重建的正确性依赖 `MessageStore.load_thread` 按写入顺序、完整吐回所有 `ResponseItem`（append-only，不去重 marker、不丢、不乱序）。不满足则下标定位不可信。默认 `JsonlMessageStore` 天然满足；业务自实现 DB store 时为协议红线。

#### Scenario: 冷加载后 rewind 历史 turn 节点
- **WHEN** 跑完 N turn 后新建 engine 传入 `initial_history`
- **THEN** `rewind_nodes()` SHALL 含所有 N turn 的 iteration + dispatch 节点，id 为 `t1:it1` … `t{N}:it{M}` 格式
- **AND** 对任意 `t{k}:it{n}` 提交 `Rewind` 应成功，不得 `rewind_rejected(unknown_node)`

#### Scenario: 压缩过的 thread 冷 rewind
- **WHEN** 包含压缩历史的 thread 冷加载后对压缩边界之后的节点提交 `Rewind`
- **THEN** rewind 成功；对被折叠 turn 的 id 得 `rewind_rejected(unknown_node)`（折叠 turn 不产节点）

### Requirement: Rewind re_reason 截到节点采样前并重采样

`mode=re_reason` 时，系统 SHALL 把 engine history 截到 `cp.history_len`、回退 `cache_anchor`，落 rewind marker（payload 含 `cut_index`），emit `turn_rewound`，再建新 root TurnRunner **从截点重采样**（LLM 自由重决下游）。

冷场景下 `_handle_rewind` SHALL 在首次操作时惰性 resolve 指令层（按构造时 entry skill），确保 re-run 带有正确的 `_last_resolved` 指令层。resolve 失败时 log warning（不 silent suppress，R3）。

#### Scenario: rewind 到 iteration 节点
- **WHEN** 对 `t1:it2` 提交 `Rewind(mode=re_reason)`
- **THEN** history 截到 `t1:it2.history_len`，重采样走出新路径（下游可与首跑不同）
- **AND** `turn_rewound.data` 含 `node_id / mode / cut_index == t1:it2.history_len`

### Requirement: Rewind retry_tool 保留派发决定、只重跑该工具

`mode=retry_tool`（仅 dispatch 节点）时，系统 SHALL 截到 `cp.inner_history_len`（**保留** assistant 的 `function_call`、丢弃旧 `function_call_output` 及其后），用 `new_args`（或原 args）**补跑该工具 / 子 skill**、追加新 output，再续推。补跑复用 `dispatch_batch` + `_build_tool_context`（含 `dispatcher`），故 `call_skill` 子 skill 也能正确重跑。

#### Scenario: retry_tool 重跑一次 call_skill
- **WHEN** 对某 dispatch 节点（如 `t1:disp1`）提交 `Rewind(mode=retry_tool)`
- **THEN** assistant 的 `function_call` 被保留；该工具被重跑、output 被替换；LLM 从新 output 续推
- **AND** `turn_rewound.data.cut_index == cp.inner_history_len`

### Requirement: thread 寻址——rewind detached spawn 子 thread

`Rewind.thread_id` 指向某 spawn 句柄的 `child_thread_id` 时，engine SHALL 路由到 `SpawnDriver.rewind_spawn`（`loop/spawn_rewind.py`，与 `Resume` 的 thread 寻址分流同形）：

1. **节点表**：`engine.rewind_nodes_for(thread_id)` 只读暴露；子 thread 节点 SHALL 从 `reconstruct_logical_history(raw)` 后的逻辑 history 经 `derive_rewind_log` 派生（**禁止对 raw 直接 derive**——坐标会错位）。
2. **活性守卫（禁状态白名单）**：拒绝按**活性**判定而非句柄状态——冷重建状态推断不产出 `error`（失败子 thread 冷启后呈现 done / running），按状态拦会挡死冷重试。放行集合 = error / done / cancelled 终态 + 中断遗留 running（不在 live 运行表）。
3. **截断**：`[rewind]` marker（`cut_index`）append 到**子 thread** store（append-only，R5）；重推以 reconstruct 后的逻辑 history 重建 detached 子 runner（`_build_child_runner`），`retry_tool` + `new_args` 只改内存 buffer、store 原样。
4. **收敛**：重推完成经 `_finalize_spawn` 单点收敛（回写句柄 + emit 终态 + barrier 幂等重查——已 fired 的 barrier 不二次触发）；重推 token 自根取消派生并登记 spawn 取消表（kill_spawn 可达，R4）。
5. **事件**：成功 emit `turn_rewound`，data **含 `thread_id`**（与根路径区分）。

典型用途：失败 spawn 的人工 retry——`error` 终态子 thread 对其最后一个 dispatch 节点 `re_reason`，LLM 重新决策失败步，前序步骤与已答 HITL 回填全部保留（业务"从失败步续跑"）。

#### Scenario: 失败 spawn 从失败步续跑
- **WHEN** spawn 句柄 status=="error"，对其子 thread 的 dispatch 节点提交 `Rewind(thread_id=child_tid, mode=re_reason)`
- **THEN** 截断到该节点采样前并重推；成功后句柄落 done + `SpawnCompleted`；再失败落 error 可再次 rewind

### Requirement: 校验失败显式拒绝（禁 silent fallback）

下列情形 SHALL emit `rewind_rejected` 并**不改 history**，绝不静默 no-op：

| reason | 触发 |
| --- | --- |
| `unknown_node` | `node_id` 不在节点表（含被折叠 turn 的节点、冷加载未传 `initial_history` 时的空表） |
| `mode_kind_mismatch` | 对非 dispatch 节点用 `retry_tool` |
| `turn_suspended` | 存在活跃挂起（HITL）record —— 挂起态 rewind v1 不支持（根 / 子 thread 同形，挂起走 Resume） |
| `unknown_thread` | `thread_id` 不属于任何 spawn 句柄的 `child_thread_id`（仅 thread 寻址路径） |
| `thread_running` | 子 thread 热跑中（live 运行表命中）或已有 rewind 在飞（仅 thread 寻址路径） |

#### Scenario: 未知节点
- **WHEN** `Rewind(node_id="does-not-exist")`
- **THEN** emit `rewind_rejected(reason="unknown_node")`，`history_snapshot()` 长度不变

#### Scenario: iteration 节点用 retry_tool
- **WHEN** `Rewind(node_id="t1:it1", mode="retry_tool")`
- **THEN** emit `rewind_rejected(reason="mode_kind_mismatch")`

## R1–R5 影响

- **R1**：`RewindCheckpoint` / `Rewind` / `reconstruct_logical_history` / `derive_rewind_log` 全通用，无业务概念；业务经 Op + `rewind_nodes()` 使用。
- **R2**：rewind 蓄意回退 anchor → 首采样 cache 失效标 **expected**（`reason="rewind"`），不计入 `unexpected_cache_breaks`。冷加载跨进程 cache 不可信，engine `__init__` 置 `_cache_anchor_index = -1`，derive 的 checkpoint `cache_anchor` 填 -1（纯保险，实际切点以 `self._cache_anchor_index` 为准）。
- **R3**：`rewind_checkpoint_recorded` / `turn_rewound` / `rewind_rejected` 三事件；新增 `RewindTableRebuilt{thread_id, turn_count, node_count}`（冷重建后 emit）。
- **R4**：重推全程透传根 `CancellationToken`，子 skill 走 `cancel.child()`；`reconstruct` / `derive` 均为同步纯 CPU，无长操作，不需要 cancel。
- **R5**：截断**仅内存**，store JSONL append-only（旧 items 不物理删），rewind / rollback marker 持久化 `cut_index`（additive payload 字段）；`reconstruct_logical_history` 只读 history，不写 store。冷重建依赖 `MessageStore.load_thread` 的「保序 + 完整」语义（协议红线，见「冷场景重建」Requirement）。

## 演示 / 参考实现

`examples/web_ui/`（demo_id `turn_rewind`）提供浏览器可交互的演示：自治链跑完后拉回访节点表，支持 re_reason / retry_tool 两种重跑模式，重跑事件经 detached bridge 实时回流前端。无 key 自动化 smoke：`examples/web_ui/smoke_detached.py`。

## 边界与暂不支持（v1）

- **call_skill 阻塞子链**的中间层 thread 不可寻址（生命周期附属父 turn、无独立句柄；thread 寻址只认 spawn 句柄的 `child_thread_id`）。
- rewind 已 done 且 barrier 已 fired 的 spawn：重推得新结果但**不自动重聚合**（fired 守卫幂等）；业务要重聚合需自行再 `set_join_barrier`。
- 多实例部署下"中断遗留 running"的活性不可见（live 运行表是单 engine 实例内闭合）；多实例互斥是业务侧部署约束。
- 挂起态 turn 内 rewind：拒绝（`turn_suspended`）。
- `retry_tool` 假定**串行派发**（`max_parallel_tool_calls=1`，默认）：并行批次内部分重试会留下「已声明未补全」的 tool_call，v1 不支持。
- replay 模式（录后确定性重放整条 call 图）、压缩等内核动作作为节点：留待后续（见设计 §8）。
- **冷 rewind 不还原历史 entry-skill 指令层**：若 thread 历史跨多个不同 entry skill 的 turn，冷 rewind 到旧 turn 时使用当前构造时传入的 entry skill 指令层，不还原"该旧 turn 当时"的指令——v1 范围约束，与「只 root turn 入表」同级。
- **自定义 CompressionStrategy 孤儿 salvage note 边界**：若自定义策略 `success=True` 却 `summary_item_id=None` 并触发了 salvage note，会写出孤儿 note；`reconstruct_logical_history` 在此情形下显式校验（而非静默误配），作为系统边界记录。内置 `sliding` / `handoff` 不触发此边界。
