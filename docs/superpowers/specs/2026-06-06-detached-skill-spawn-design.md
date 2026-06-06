# 设计:分离式 skill spawn + join-barrier(并发多专家 · 各自独立 HITL · 收齐聚合)

- 日期:2026-06-06
- 状态:待 review
- 适用红线:R1(业务零侵入)/ R2(cache 友好)/ R3(可观测)/ R4(可取消)/ R5(可 resume + 冷恢复)—— 改并发派发 / suspend 路由,按 CLAUDE.md 逐条声明影响(见 §6)

## 1. 问题陈述

主 LLM 读一个 case 后,想**并发起多个专家**(可同 skill 多实例,如「规划 3 条路线」),每个专家可能**各自需要 HITL**、**各自完成**,最后交一个「联合会诊」skill 汇总,再整体结束。需要两种发起姿态都支持:

- **等待收齐**:并发跑、一起收。——现有并发 `call_skill` 批次已支持(`max_parallel_tool_calls>1` + call_skill 跳锁),但其 HITL 是**批次栅栏**(`dispatch_batch` 用 `asyncio.gather`):同批多个专家要 HITL 时**一起挂、一次 resume**,无法「A 完成、过一会 B 才 HITL」。
- **各自发起**:每个专家独立跑/独立 HITL/独立完成,**错峰**。——当前**不支持**(批次栅栏 + 禁部分 resume,见 `suspend/resolver.py`)。

本设计补「各自发起」为一等能力,并提供 join-barrier 拿到「收齐即聚合」的效果而**不引入 parked turn**。

> 对照:模型 B(父 turn 长活、阻在 join、引擎交错喂 resume)能做到同 turn 内联收齐+带 HITL,但**直接违背内核「挂起即结局、不阻塞、实例可释放」**、且毁掉跨进程 resume。故采用**模型 A(分离式 spawn)+ join-barrier** 取其效果、避其代价。

## 2. 目标与非目标

**目标**
- 内核提供**分离式 sub-skill spawn**:`spawn_skill` 立即返回句柄、不阻塞发起 turn;每个 spawn 是独立 child thread,**各自独立 suspend-resume / 完成**。
- **join-barrier**:登记「{句柄集} 全终态 → 自动起聚合 skill turn」,拿到「收齐即会诊」效果,无 parked turn。
- LLM 工具 + 业务 API **双入口**,共享同一底层。
- **冷恢复**:句柄/屏障/child threads 全落 store,进程重启可重建续跑。

**非目标(v1 不做)**
- 不做模型 B(parked 父 turn + 交错 resume)。
- 不改 `call_skill` 批次语义(「等待收齐 + 无 HITL」继续用它)。
- 不改声明式 orchestration(`orchestration:` parallel/serial/when 仍是确定性、HITL-free 范式;其挂起缺口不在本设计范围)。
- barrier 仅「全终态触发」;any / 超时触发留后续。
- mid-flight 被中断专家的自动断点续跑(只保证 suspended/done 两态干净恢复)。

## 3. 核心模型:句柄化的 detached child session

```
发起 turn(entry LLM 或业务):
  spawn_skill(专家A) → handleA(立即返回,非阻塞)  ┐ 各起独立 child thread + 分离任务
  spawn_skill(专家B) → handleB                     │ (复用 call_skill 的子 TurnRunner@child thread,
  spawn_skill(专家C) → handleC                     ┘  差别:分离、不阻塞父 turn)
  await_skills([A,B,C] → 联合会诊)  ← 可选 join-barrier
  turn 结束(不阻塞,引擎按引用计数保活)

各自独立、错峰:
  A 跑→HITL(自己 thread 落 record,发 spawn_suspended)→Resume(A)→完成→spawn_completed
  B 跑→完成
  C 跑→HITL→Resume→完成
  ↓ 最后一个 spawn_completed:barrier 句柄全终态 → 自动 submit 聚合 turn(联合会诊)
  联合会诊完成 → 业务整体结束(root turn_completed)
```

### 构件

| 构件 | 职责 | 位置 |
| --- | --- | --- |
| `SpawnHandle` | `{handle_id, skill_id, child_thread_id, status(running/suspended/done/error/cancelled), result}` | `loop/spawn_handle.py`(新) |
| `SpawnHandleRegistry` | 句柄登记 / 查询 / 状态更新;可由 parent thread 项重建(冷恢复) | engine 持有 |
| `spawn_skill`(工具)+ `engine.spawn_skill()` | 过 DispatchPolicy + SpawnSlot(K1)→ 建 child thread + 分离任务 → 登记句柄 → **立即返回** | `tool/builtins/spawn_skill.py`(新)+ engine |
| `JoinBarrier` / barrier 触发器 | 登记 `{handle_ids, then_skill_id, args_template}`;每次 spawn_completed 检查全终态 → 自动起聚合 turn | engine |
| `await_skills`(工具)+ `engine.set_join_barrier()` | 注册 barrier | 同上 |
| `join_skill`(工具,后续 turn)+ `engine.spawn_status()` | **非阻塞**读句柄结果(ready/pending) | 同上 |
| `kill_skill`(工具)+ `engine.kill_spawn()` | R4 杀单个 spawn | 同上 |

### 身份与 resume 路由(内部形态)

`handle_id ↔ child_thread_id` 一一对应;每 spawn = **parent engine 下的独立 child thread + 分离 asyncio 任务**(复用 call_skill 已有「子 TurnRunner@child thread」机制 + `_handle_child_resume`)。

- **engine 生命周期改引用计数**:父 turn 结束**不**立即释放;只要有未终结 detached child(running/suspended)或未触发 barrier 就保活;全终结 → 可释放。
- **HITL 路由**:专家在自己 child thread 落挂起 record,发 `spawn_suspended`(= 该 child thread 的 turn_suspended,data 带 `handle_id` + `thread_id`)。业务 `Resume(thread_id=child, resolutions=…)` → 走**放宽前提的 `_handle_child_resume`**(支持对一个 detached child 独立续跑,不要求父 turn 处于挂起)→ 续到完成 → `spawn_completed`。
- **barrier 触发**:`spawn_completed`/`spawn_failed` 后检查所属 barrier 句柄集是否全终态 → 是则用 `args_template` 填各专家结果,自动 `submit` 聚合 turn,落 `join_barrier_fired` 标记并发事件。

> 为何用「child thread of parent engine」而非「pool 里另起 sibling session」:前者复用 call_skill child-thread 派发 + `_handle_child_resume` + K1 配额(都在 engine 内),改动面小;后者要把句柄/屏障上提 pool 层、spawn 工具反向够到 pool,耦合更重。代价:engine 从「绑死一个 thread」放宽到「父 thread + 若干 detached child」+ 引用计数保活。

## 4. 接口

| 能力 | LLM 工具 | 业务 API | in / out |
| --- | --- | --- | --- |
| 分离发起 | `spawn_skill` | `engine.spawn_skill(...)` | in `{skill_id, args?, reason}`(reason 必填);out `{handle_id, child_thread_id}` |
| 登记屏障 | `await_skills` | `engine.set_join_barrier(...)` | in `{handle_ids:[...], then_skill_id, then_args_template?}`;out `{barrier_id}` |
| 主动收 | `join_skill` | `engine.spawn_status(...)` | in `{handle_ids:[...], mode: all\|any\|ready}`;out 各句柄 status+result(未齐返回 pending) |
| 取消 | `kill_skill` | `engine.kill_spawn(handle)` | R4,杀单个不伤兄弟 |

## 5. 新事件(R3)

`spawn_started{handle_id,skill_id,child_thread_id}` / `spawn_suspended{handle_id,thread_id,pending}` / `spawn_completed{handle_id,result}` / `spawn_failed{handle_id,error}` / `spawn_cancelled{handle_id}` / `join_barrier_registered{barrier_id,handle_ids,then_skill_id}` / `join_barrier_fired{barrier_id,then_thread_id}`。并入 `MsgKind` + `Msg` union。

## 6. R1–R5 影响声明

- **R1**:全通用结构,无业务概念。✅
- **R2**:spawn 仅往 parent history **尾部**追加句柄项(同 tool call,不动 head);child thread 自带独立 cache 生命周期;spawn 不触发压缩。✅
- **R3**:§5 七类事件。✅
- **R4**:每 spawn `cancel.child()`;`kill_skill` 杀单个;shutdown 级联取消所有 detached child。✅
- **R5**:句柄(`spawn` 项)+ 屏障(`join_barrier` 项)+ 触发幂等(`join_barrier_fired` 标记)全落 parent thread(append-only);child threads 自带 suspend-resume。重载可重建 registry+barriers 并续跑。✅

## 7. 冷恢复

**落盘**(append-only,记 parent thread):spawn → `spawn` 项;`await_skills` → `join_barrier` 项;触发 → `join_barrier_fired` 标记(幂等锚)。

**重载**(engine 加载 parent thread 后):
1. 扫项 → 重建 `SpawnHandleRegistry` + barriers。
2. 每 handle status 由其 child thread 终态推定(末条挂起 record=suspended / turn_completed=done / 无终态=mid-flight 中断)。
3. suspended 专家 → 等 `Resume(child_thread)` 续跑;done 专家 → 结果可读。
4. barrier 句柄全终态且无 `fired` 标记 → 立即触发聚合(幂等)。

## 8. 错误边界(禁 silent fallback)

| 场景 | 行为 |
| --- | --- |
| spawn 未知 skill / 非白名单 / 超深度 / 成环 | `ToolResult.error(<reason>)`,不建 child |
| SpawnSlot(K1)超限 | `spawn_limit_exceeded`,显式拒 |
| `await_skills` 含未知 handle / `then_skill_id` 不可作 entry | **注册期拒绝**(不登记永不可触发的屏障) |
| 某专家 `spawn_failed` | barrier 在全句柄到**终态(done/error/cancelled)**才触发;聚合 skill 收到每句柄终态+结果(含失败),**不静默丢失败专家** |
| `join_skill` / `kill_skill` 未知 handle | 显式 error |
| `Resume` 非挂起的 child thread | 走现有 `no_active_suspension` 拒绝 |

## 9. 测试(边界必测,全 MockClient)

`tests/loop/test_detached_spawn.py`:
1. `test_spawn_returns_handle_nonblocking` —— spawn 立即返回 handle,发起 turn 不阻塞即结束。
2. `test_spawn_independent_completion_events` —— 2 个 spawn 各自 `spawn_completed`,handle/thread 区分。
3. `test_spawn_staggered_hitl` —— 专家A HITL→Resume(A)→完成,之后专家B HITL→Resume(B)→完成(错峰,互不耦合);各自 `spawn_suspended`/`spawn_completed`。
4. `test_same_skill_multiple_instances` —— 同 skill spawn 3 实例(规划 3 路线),3 个独立 handle/thread。
5. `test_join_barrier_fires_when_all_done` —— 全句柄 done → 自动起聚合 turn,`join_barrier_fired` + 聚合 skill 收到各结果。
6. `test_join_barrier_with_failed_expert` —— 一个专家失败 → barrier 仍触发,聚合收到失败终态(不静默丢)。
7. `test_join_skill_nonblocking_status` —— 后续 turn `join_skill` 读 ready/pending。
8. `test_kill_spawn_isolates` —— kill 一个 handle 不影响兄弟(R4)。
9. `test_spawn_quota_rejected` —— 超 K1 配额显式拒。
10. `test_spawn_unknown_skill_rejected` / whitelist / cycle 拒绝路径。
11. `test_cold_recovery_rebuilds_handles_and_barrier` —— spawn+barrier 后释放 engine,重新加载 → registry/barrier 重建;suspended 专家可 Resume;全 done 后 barrier 幂等触发(不重复)。

## 10. 文档义务(收尾红线)

- 新增 `docs/architecture/capabilities/detached-spawn.md` 契约。
- 更新 `docs/architecture/agent-loop.md`(spawn/barrier/引用计数生命周期 + 新 Op/工具)、`skill-system.md` 若涉及 dispatch 共用闸。
- 新增 ADR(为何模型 A+barrier 而非 B、child-thread vs sibling-session、barrier 全终态策略)。
- `docs/configurable-knobs.md` 补 spawn 工具 / API / barrier。
- 新增 `examples/multi_expert_consult/`(身体情况 → 并发多专家含错峰 HITL → 联合会诊,打印事件时间线)。
