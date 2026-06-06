# Capability: detached-spawn

## Purpose

主 LLM（或业务 API）在一次 turn 内**分离式**地并发启动多个专家子 skill，每个 spawn 立即返回句柄、不阻塞父 turn；每个专家在**独立 child thread + 独立 asyncio 任务**中运行，可**各自独立 HITL / 各自独立完成**（错峰），无需同批挂起 / 同批 resume。

可选地登记一个 **join-barrier**：当 `handle_ids` 里全部句柄到达终态（done / error / cancelled），内核自动在父 engine 内发起一次聚合 skill turn，不需要 parked 父 turn 阻塞等待。

设计 spec：`docs/superpowers/specs/2026-06-06-detached-skill-spawn-design.md`

实现：
- `src/taifeng/loop/spawn_driver.py`（`SpawnDriver`：spawn 状态 + 核心逻辑）
- `src/taifeng/loop/spawn_handle.py`（`SpawnHandle`、`SpawnHandleRegistry`、`JoinBarrier`）
- `src/taifeng/loop/engine.py`（薄转发层：`spawn_skill / set_join_barrier / spawn_status / kill_spawn / has_live_spawns`）
- `src/taifeng/loop/event.py`（7 类新事件）
- `src/taifeng/tool/builtins/`（4 个 LLM 工具：`spawn_skill / await_skills / join_skill / kill_skill`）

## 数据契约

### `SpawnHandle`（`loop/spawn_handle.py`）

单个 detached spawn 的句柄；append-only 不破（R5）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `handle_id` | `str` | 父 engine 内稳定唯一 id（UUID） |
| `skill_id` | `str` | 被 spawn 的 skill id |
| `child_thread_id` | `str` | 独立 child thread id；`Resume` 路由用此字段 |
| `status` | `Literal["running","suspended","done","error","cancelled"]` | 当前状态 |
| `result` | `str \| None` | 终态结果（done → assistant 最终文本 `outcome.final_text`；error → error 信息字符串；cancelled / running / suspended → None） |

**状态转换**：

```
running → suspended（HITL）→ running（Resume）→ done | error | cancelled
running → done | error | cancelled
```

### `JoinBarrier`（`loop/spawn_handle.py`）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `barrier_id` | `str` | 唯一 id |
| `handle_ids` | `frozenset[str]` | 需全部到达终态才触发 |
| `then_skill_id` | `str` | 聚合 skill id（注册时校验存在性，**不校验** entry 资格） |
| `then_args_template` | `dict \| None` | `None` → 聚合 args = `{handle_id: {status, result}}` for 全部句柄（含 failed / cancelled，不丢弃）；非 None → 模板 dict **原样透传**为聚合 turn 的种子 args（v1 **不做**任何 `{{handle_id}}` 占位替换） |

### SpawnHandle 落盘（append-only ResponseItem）

| item type | 落盘时机 | 内容 |
| --- | --- | --- |
| `spawn` | `spawn_skill` 调用成功后追加到**父 thread** | `{handle_id, skill_id, child_thread_id}` |
| `join_barrier` | `set_join_barrier` 注册后追加到**父 thread** | `{barrier_id, handle_ids, then_skill_id}` |
| `join_barrier_fired` | barrier 触发后追加（幂等锚） | `{barrier_id, then_thread_id}` |

冷恢复靠这三类 item 重建全部状态（见「Requirements: 冷恢复」）。

### 7 个 EventMsg（`loop/event.py`）

| kind | 触发时机 | data 关键字段 |
| --- | --- | --- |
| `spawn_started` | spawn 分离任务启动后 | `handle_id, skill_id, child_thread_id` |
| `spawn_suspended` | spawn 内部 HITL 挂起，任务退栈 | `handle_id, thread_id, pending` |
| `spawn_completed` | spawn 正常结束 | `handle_id, result` |
| `spawn_failed` | spawn 以 error 终止 | `handle_id, error` |
| `spawn_cancelled` | `kill_spawn` / 根取消 触发 | `handle_id` |
| `join_barrier_registered` | `set_join_barrier` 注册成功 | `barrier_id, handle_ids, then_skill_id` |
| `join_barrier_fired` | 全部句柄终态，聚合 turn 已提交 | `barrier_id, then_thread_id` |

## Requirements

### Requirement: spawn_skill 立即返回、不阻塞发起 turn

`spawn_skill(*, skill_id, args, reason)` SHALL 立即返回 `{handle_id, child_thread_id}`；调用方 turn 继续执行，不等待子 skill 完成。每个 spawn 是**独立 child thread + 独立 detached asyncio 任务**；cancel token = `root_cancel.child(f"spawn:{handle_id}")`。

调用前经过三道门控（任一失败 → ValueError / SpawnLimitError，不建 child thread）：
1. **unknown_skill**：skill_id 不在 snapshot → `ValueError`
2. **DispatchPolicy.check**：白名单 / 深度 / 环 / 不能 call entry skill → `ValueError("dispatch_rejected: <reason>")`
3. **K1 SpawnSlotRegistry**：并发 running spawn 已达 `max_concurrent_spawns` 或累计达 `max_total_spawns` → `SpawnLimitError`

成功后追加 `spawn` ResponseItem 到父 thread（冷恢复用），emit `spawn_started`。

#### Scenario: spawn 立即非阻塞返回

- **WHEN** 业务 API `engine.spawn_skill(skill_id="expert-a", args={}, reason="并发分析")` 调用
- **THEN** 立即返回 `{handle_id, child_thread_id}`，发起 turn 在 spawn 完成前已结束
- **AND** emit `spawn_started{handle_id, skill_id, child_thread_id}`
- **AND** 父 thread 末尾追加一条 `spawn` ResponseItem（R5）

#### Scenario: 拒绝路径

| 触发 | 拒绝原因 |
| --- | --- |
| 未知 skill_id | `ValueError`，不建 child thread |
| 非白名单 / 超深度 / 成环 | `ValueError("dispatch_rejected: <reason>")` |
| 不能 call entry skill | `ValueError("dispatch_rejected: cannot_call_entry_skill")` |
| 并发 K1 超限 | `SpawnLimitError` → 上层转 `SkillSpawnRejected` 事件 |

### Requirement: 各自独立 HITL — staggered Resume 路由

每个 spawn 的 HITL 挂起记录落**该 spawn 的 child thread**，不落父 thread。`spawn_suspended{handle_id, thread_id, pending}` 发出后，该 spawn 的 detached asyncio 任务**退栈**（父 turn 不受影响，父 thread 无 pending gap）。

业务凭 `Resume(thread_id=<child_thread_id>, resolutions={request_id: payload})` 提交。Engine 在 `Resume` 分发时：

1. 用 `thread_id` 匹配 `SpawnHandleRegistry`：命中挂起态句柄 → 路由到专用 `SpawnDriver.resume_spawn`
2. 未命中已知 spawn → 走原有父链 `_handle_resume` / `_handle_child_resume`（对称：detached spawn 的 `Resume` 走 spawn 路径，`call_skill` 嵌套挂起走父链路径）

`resume_spawn` 内部：
- 复用 `SuspensionResolver`（强制全量 resume，禁部分 resume）
- 复用 `_build_child_runner`（call_stack 为空 → 子 turn 是**独立根 turn**，无 DispatchPolicy entry 门控）
- 复用 `_finalize_spawn`（终态回调 + barrier 检查，与首发路径完全一致）

**关键**：不复用 `_handle_child_resume`，因为后者假设父 turn 此刻仍挂在 `CHILD_SKILL` pending gap 上并需要沿链回填；detached spawn 的父 turn 早已结束，不存在这条链。

再次挂起（多轮 HITL）→ 句柄重标 `suspended`，可再 `Resume`（无限轮次，直到终态）。

#### Scenario: 错峰 HITL

- **GIVEN** spawn A、B 均在运行
- **WHEN** A 先 HITL → emit `spawn_suspended{handle_id=A}`；B 继续运行并完成 → emit `spawn_completed{handle_id=B}`
- **THEN** `Resume(thread_id=A_child_thread_id)` 仅恢复 A，B 不受影响
- **AND** A 完成后 emit `spawn_completed{handle_id=A}`

#### Scenario: 多轮错峰 HITL

- **WHEN** A Resume 后再次 HITL
- **THEN** emit 第二条 `spawn_suspended{handle_id=A}`，handle 状态回 `suspended`
- **AND** 再次 `Resume(thread_id=A_child_thread_id)` 仍可续跑

### Requirement: join-barrier 全终态自动触发聚合

`set_join_barrier(handle_ids, then_skill_id, then_args_template=None)` 登记一道屏障：

- 注册时校验：每个 handle_id 已知 + `then_skill_id` 在 snapshot 中存在（不校验 entry 资格）
- 返回 `{barrier_id}`，emit `join_barrier_registered`，追加 `join_barrier` ResponseItem 到父 thread

触发条件：`handle_ids` 内**全部**句柄到达终态（done / error / cancelled）。任意一个句柄到达终态后检查；全部终态才触发。

触发时：
- `then_args_template=None` → 聚合 args = `{handle_id: {status, result}}` for **全部** handles（含 failed / cancelled，不丢弃）
- 聚合 turn 通过 `_build_child_runner`（call_stack 为空）以**独立根 turn**发起
- 追加 `join_barrier_fired` 标记（幂等：`_fired_barriers` 内存集合 + store 标记，重复触发 no-op）
- emit `join_barrier_fired{barrier_id, then_thread_id}`

**v1 仅支持「全终态」触发**；any / 超时触发留后续。

#### Scenario: 全 done 触发聚合

- **GIVEN** barrier 登记了 handles `[A, B, C]`，then_skill_id = `"joint-review"`
- **WHEN** A、B、C 全部完成（done）
- **THEN** 自动起一次 `"joint-review"` skill turn，args = `{A: {status:"done",result:...}, B:..., C:...}`
- **AND** emit `join_barrier_fired{barrier_id, then_thread_id}`
- **AND** `join_barrier_fired` 标记落父 thread，重复 check 幂等

#### Scenario: 含失败专家仍触发聚合

- **WHEN** A 失败（error）、B / C 正常完成（done）
- **THEN** barrier 在最后一个到达终态时仍触发
- **AND** 聚合 args 含 `{A: {status:"error", result:{...error info...}}, B:..., C:...}`（不静默丢 A）

### Requirement: engine keepalive 保活

`has_live_spawns()` 为 `True`（即有 status ∈ {running, suspended} 的句柄）时，`pool.release(session_id)`（非 force）是**空操作**——engine 继续缓存运行，不释放。

只有 `pool.close()` 或 `pool.release(force=True)` 才无条件拆除，同时级联取消全部 detached child。

#### Scenario: 父 turn 结束后 engine 保活

- **GIVEN** engine 有 2 个 running spawn
- **WHEN** 业务调用 `pool.release(session_id)`（非 force）
- **THEN** engine 不被释放；`has_live_spawns()` 仍为 `True`
- **AND** 等全部 spawn 终态后，下次 `release` 才真正释放

### Requirement: kill_spawn 隔离取消

`kill_spawn(handle_id)` 只取消目标 spawn 的 `CancellationToken`，其他 sibling spawn 不受影响。

| handle_id 状态 | 行为 |
| --- | --- |
| 未知 | raise `KeyError` |
| 终态（done / error / cancelled） | 空操作（benign no-op） |
| running / suspended | 取消该 spawn 的 token → 触发 `spawn_cancelled` |

#### Scenario: kill 单个不影响兄弟

- **GIVEN** spawn A、B 均在 running
- **WHEN** `kill_spawn(A.handle_id)`
- **THEN** emit `spawn_cancelled{handle_id=A}`；B 继续运行

### Requirement: spawn_status 非阻塞读

`spawn_status(handle_ids)` → `{hid: {status, result}}`

- 对于未知 `handle_id` → `{"status":"unknown","result":None}`（不 raise）
- 纯只读，不阻塞，不改变任何状态

### Requirement: 冷恢复（R5）

进程重启、同 session 重载后，`SpawnDriver.rebuild_from_history` 扫描父 thread items，重建：

1. `SpawnHandleRegistry`（从 `spawn` items）：每个 handle status 由对应 child thread 终态推断：
   - child thread 末条 item 为活跃挂起 record → `suspended`
   - child thread 末条 item 为 assistant_message（done）→ `done`
   - 无终态 item → `running`（mid-flight 中断，v1 限制：不自动重驱动，留为 best-effort）
2. barriers（从 `join_barrier` items）
3. `_fired_barriers`（从 `join_barrier_fired` markers，幂等）
4. 调用 `_check_barriers()`（幂等）：若某 barrier 全部句柄已终态且未 fired → 立即触发聚合

**v1 限制**：mid-flight 中断（重启时 status 推为 running）的 spawn 不自动重驱动，需业务侧干预。

## R1–R5 影响

- **R1**：`SpawnHandle` / `JoinBarrier` / 4 个 LLM 工具全通用，无业务概念；spawn 目标为 entry:false child skill（与 `call_skill` 相同规则，专家 skill 不应是 entry）；业务侧通过 `ctx.extras["spawn_coordinator"]` 经 engine 注入的协调器接口使用。
- **R2**：spawn 仅往父 history **尾部**追加 `spawn` ResponseItem（同 tool_call，不动 head）；child thread 有独立 cache 生命周期；spawn 本身不触发压缩。suspended spawn 释放其 K1 并发 slot（不计入 `max_concurrent_spawns`，只有 running 的 spawn 占 slot），即 HITL 等待期不消耗并发额度。
- **R3**：7 类事件（spawn_started / suspended / completed / failed / cancelled / join_barrier_registered / join_barrier_fired）均入 `EventMsg`，经 `TelemetrySink` 可观测。
- **R4**：每个 spawn `cancel.child(f"spawn:{handle_id}")`；`kill_spawn` 杀单个，兄弟 spawn 不受影响；`pool.close()` / `release(force=True)` 级联取消全部 detached child。
- **R5**：`spawn` / `join_barrier` / `join_barrier_fired` 三类 ResponseItem 落父 thread（append-only，不改已有 items）；child threads 自带 suspend-resume；进程重启后 `rebuild_from_history` 重建 registry + barriers + fired 集合，suspended 专家可继续 Resume，done 专家结果可读，barrier 幂等触发。

## v1 边界

| 限制 | 描述 |
| --- | --- |
| mid-flight 中断 spawn | 重建时状态推为 `running`，但不自动重驱动；需业务侧干预（下一版本补 checkpoint 断点续跑） |
| suspended spawn 释放 K1 slot | suspended 状态下 slot 已释放（slot 仅计算飞行中 runner，run() 退栈即释放）；lifetime cap `max_total_spawns` 仍单调递增不回收 |
| barrier 仅全终态触发 | any / 超时触发留后续 |
| 无 model-B parked turn | 父 turn 不阻塞等待；「同 turn 内 join 结果后继续 LLM 推理」需业务侧在聚合 skill 内完成 |
| then_skill_id 不校验 entry 资格 | 聚合 turn 通过 `_build_child_runner`（call_stack 为空）发起，无 DispatchPolicy entry 门控；then_skill_id 可为 entry:true 或 entry:false，只校验存在性 |
