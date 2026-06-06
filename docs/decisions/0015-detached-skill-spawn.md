# ADR 0015: detached-skill-spawn —— 分离式并发 spawn + join-barrier

- 状态：Accepted
- 日期：2026-06-07
- 关系：不推翻任何 ADR；与 ADR 0006（统一 skill 模型 / entry 不变量）、ADR 0012（suspend-resume 原语）、ADR 0013（子 thread 嵌套挂起续跑链）协作

## 背景

主 LLM 在一次 turn 内想并发起多个专家子 skill（可同 skill 多实例），每个专家：
- 可能**各自独立 HITL**（错峰，不同时挂起 / 不同时 resume）
- 各自独立完成后**汇聚**到一次联合会诊 skill

现有并发 `call_skill` 批次（`max_parallel_tool_calls>1`）支持「等待收齐 + 无 HITL」，但：
- 批次挂起是**栅栏**（`asyncio.gather` → 同批全部挂起、必须同批 resume），无法错峰
- `SuspensionResolver` 禁部分 resume，同批多 HITL 必须提供所有 resolution 才能续跑

两种姿态：

| 姿态 | 现状 | 本设计 |
| --- | --- | --- |
| 等待收齐 + 无 HITL | call_skill 并发批次已支持 | 不变 |
| 各自独立 HITL + 错峰收齐 | **不支持** | 本 ADR：detached spawn + join-barrier |

## 决策

### 1. 模型 A（detached spawn）+ join-barrier，而非模型 B（parked 父 turn）

**模型 B**：父 turn 长活、阻在 join 点等待子 skill 全部完成（交错喂 resume）。

**拒绝模型 B** 的原因：
- 直接违背内核「挂起即结局、不阻塞、实例可释放」原则（ADR 0012 核心约束）
- 父 turn 阻塞期间 engine 实例不可释放，破坏跨进程 resume
- engine 需要同时处理多个「交错 resume」，serialized 的 actor 队列变成并发处理多个挂起父链，引入竞态

**模型 A（本设计）**：
- spawn 立即返回句柄，父 turn 正常结束（不阻塞）
- 每个 spawn 是独立 child thread + 独立 detached asyncio 任务，各自独立 HITL / 完成
- join-barrier 在内核侧「全终态自动触发聚合 skill turn」，无需业务侧轮询

### 2. child-thread-of-parent-engine，而非 pool 层 sibling-session

**拒绝 sibling-session** 的原因：
- 句柄 / 屏障需上提 pool 层，spawn 工具需反向够到 pool，耦合重
- K1 配额（`max_concurrent_spawns`）需跨 session 计数，pool 层需感知 engine 内部状态
- `_build_child_runner` / `_finalize_spawn` 的实现在 engine 内，复用需跨层拉依赖

**采用 child-thread-of-parent-engine**：
- 复用 call_skill 已有「子 TurnRunner @ child thread」机制
- `SpawnDriver` 直接持有 `SpawnHandleRegistry`，K1 在 engine 内统一计数
- 代价：engine 从「绑死一个 thread」放宽到「父 thread + 若干 detached child」，需引用计数保活

### 3. 专用 resume_spawn，而非复用 _handle_child_resume

`_handle_child_resume` 假设父 turn 此刻仍挂在 `CHILD_SKILL` pending gap 上，沿 CHILD_SKILL pending 串链从根探到叶，并逐层把子结果回填父 `call_skill` 的 `function_call_output`，最终重推根 turn。

detached spawn 的父 turn **早已结束**，不存在这条续跑链。强行复用需给 `_handle_child_resume` 塞「无父链」分支，与现有「有父链」逻辑耦合，把已很复杂的续跑链搅浑。

专用 `resume_spawn` 做法：
1. 复用 `SuspensionResolver`（强制全量 resume，禁部分 resume）
2. 复用 `_build_child_runner`（call_stack 为空 → 子 turn 是独立根 turn，无 DispatchPolicy entry 门控）
3. 复用 `_finalize_spawn`（与首发路径完全一致）

新增代码只是把复用组件按「无父链的独立 child」编排到一起，与 `_handle_child_resume` 零重叠。

### 4. 聚合 turn 走 _build_child_runner（call_stack 为空），不经 DispatchPolicy entry 门控

barrier 的 `then_skill_id` 注册时只校验存在性，不校验 entry 资格。聚合 turn 通过 `_build_child_runner`（call_stack 为空）以独立根 turn 发起，与 DispatchPolicy 的 `cannot_call_entry_skill` 约束无关——该约束只针对 `call_stack` 非空（嵌套调用）的场景。

因此 `then_skill_id` 可为 `entry:true` 或 `entry:false`，业务侧可灵活选择。

### 5. barrier 仅「全终态」触发

any / 超时触发的语义与「容错 / 抢先收结果」的业务策略高度相关，留业务层决定。内核仅提供最简洁确定的语义（全终态）。

### 6. K1 并发配额仅计 running spawn，suspended 释放 slot

`max_concurrent_spawns` 只统计 **`runner.run()` in-flight** 的 spawn。spawn HITL 挂起时 runner 退栈，slot 随即释放；resume 时重新占用。

原因：
- HITL 等待期是 IO 等待（等人），不占 LLM / CPU 资源；算进并发会人为降低吞吐
- 与 `call_skill` 现有「挂起不计 tool slot」一致
- `max_total_spawns` 仍单调递增，作为「总量兜底」防 runaway 循环

### 7. 默认 then_args_template=None → 含 failed/cancelled 全量传递

聚合 skill 应能**感知所有专家的终态**（含失败），以便做「部分失败容错」策略。
丢弃失败专家结果会触发 silent fallback（CLAUDE.md 红线），故默认传全量。

## 影响

- **R1–R5**：见 [`capabilities/detached-spawn.md`](../architecture/capabilities/detached-spawn.md) 「R1–R5 影响」。关键：R2 suspended spawn 释放 K1 slot；R5 spawn/join_barrier/join_barrier_fired 三类 item append-only 落父 thread；R4 kill_spawn 仅取消目标 token。
- **与 ADR 0006 的关系**：spawn 目标遵循同一约束——专家 skill 应为 `entry:false` child skill（与 `call_skill` 相同规则）；entry 不变量不放松。barrier 聚合 turn 走空 call_stack 独立根 turn，不受 `cannot_call_entry_skill` 约束。
- **新增**：`SpawnDriver` / `SpawnHandle` / `SpawnHandleRegistry` / `JoinBarrier`；engine 薄转发层；4 个 LLM 工具（spawn_skill / await_skills / join_skill / kill_skill，均 `parallel_safe=True`，通过 `ctx.extras["spawn_coordinator"]` 接入 engine）；7 个 EventMsg；`has_live_spawns()` keepalive 引用计数；冷恢复 `rebuild_from_history`。
- **v1 暂不支持**：mid-flight 中断 spawn 自动重驱动；any / 超时 barrier 触发；模型 B（parked 父 turn）。

## 参照

设计 spec：`docs/superpowers/specs/2026-06-06-detached-skill-spawn-design.md`

参照实现（只学范式）：
- codex `agent/registry.rs::reserve_spawn_slot`：K1 广度准入
- claw-code `ConversationRuntime` detached task：分离异步任务模式
