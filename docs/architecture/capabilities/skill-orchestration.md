# Capability: skill-orchestration（声明式 skill 编排）

## Purpose

composite skill 可在 SKILL.md 声明子步骤的并行/顺序/条件编排；缺省回退 LLM 自主决策。执行层复用 [并发 fan-out 派发 A]。

## Requirements

### Requirement: 可选 orchestration 声明字段

`SkillDefinition` SHALL 新增可选字段 `orchestration`（默认 None）。其 SHALL 仅在 `type == "composite"` 上出现；atomic skill 声明该字段时，`validate()` SHALL raise `SkillValidationError`。

`orchestration` 结构 SHALL 为 `{ steps: list[Step] }`，其中 `Step` ∈ 三种原语之一：
- `{ parallel: list[skill_id] }`：同批并发派发
- `{ serial: list[skill_id] }`：顺序执行
- `{ when: { condition: str, then: Step叶子, else?: Step叶子 } }`：单层条件分支

#### Scenario: atomic 声明 orchestration 被拒
- **WHEN** atomic skill 的 SKILL.md 含 `orchestration` 字段
- **THEN** 启动期 `validate()` SHALL raise `SkillValidationError`

#### Scenario: 未声明则缺省回退
- **WHEN** composite skill **未**声明 `orchestration`
- **THEN** turn 行为 SHALL 与当前实现完全一致（LLM 在 `_sample_once` 自主 call_skill + A 的隐式并发），无任何可观测差异

### Requirement: 编排引用校验

启动期 loader SHALL 校验 `orchestration.steps` 内所有 skill_id：
- 每个 id SHALL ∈ 声明者的 `child_skills` 白名单；否则报错
- 引用 SHALL NOT 引入调用环（复用 `detect_cycles`）
- 同一 id 在单个 `parallel` 组内 SHALL NOT 重复

#### Scenario: 引用未在白名单的 child
- **WHEN** `orchestration` 引用了不在 `child_skills` 的 skill_id
- **THEN** 启动期 SHALL raise 校验错误（fail-fast，不延迟到运行时）

### Requirement: 执行复用 A 的 dispatch_batch

当 entry skill 声明了 `orchestration` 时，引擎 SHALL 按 `steps` 顺序驱动（段间串行），其中 `parallel` 组 SHALL 通过 A 的 `tool_batch.dispatch_batch`（受 `max_parallel_tool_calls` 限流）执行。引擎 SHALL NOT 为编排引入第二套并发原语。

#### Scenario: parallel 组并发执行
- **WHEN** 一个 `parallel: [a, b]` 段被执行且 `max_parallel_tool_calls >= 2`
- **THEN** a、b 的 sub-turn SHALL 并发执行（wall-clock < 串行和），且历史按发起序配对回填（继承 A 的不变量）

#### Scenario: serial 段串行执行
- **WHEN** 一个 `serial: [a, b]` 段被执行
- **THEN** b 的 sub-turn SHALL 在 a 完成之后才开始

### Requirement: 条件 flag 禁 silent fallback

`when.condition` 引用的布尔 flag SHALL 由上一步 sub-skill 的结构化输出显式产出。flag 缺失或非布尔时，引擎 SHALL emit `orchestration_condition_missing` 事件并按领域错误处理；SHALL NOT 静默回退为 true/false 继续。

#### Scenario: 条件 flag 缺失
- **WHEN** `when.condition` 引用的 flag 在上一步输出中缺失
- **THEN** 引擎 SHALL emit `orchestration_condition_missing{skill_id, condition}` 且 SHALL NOT silent fallback

### Requirement: 可观测

编排声明被解析为执行计划时，引擎 SHALL emit `orchestration_plan_resolved{skill_id, groups}`。并发执行仍 SHALL 复用 A 的 `tool_batch_dispatched` / 逐 call `tool_call_started`/`tool_call_completed`。

#### Scenario: 计划解析事件
- **WHEN** 一个声明了 `orchestration` 的 entry skill 开始执行
- **THEN** SHALL emit 一次 `orchestration_plan_resolved`，data 含 skill_id 与解析出的步骤分组结构

### Requirement: 子挂起传递与重入重放（orchestration-suspension-propagation）

编排批内子 skill 挂起 SHALL 正确上浮：`_execute_leaf` 按 `DispatchOutcome.suspend` 二分——完成子照常 (fc, fco) 配对回填；挂起子 SHALL 只追加悬空 fc（占位文本 `"<suspended>"` SHALL NOT 入史）；批内任一挂起 → 抛 `_BatchSuspend` 由 run() 既有路径落盘挂起，编排 turn 以 suspended 终结（与 LLM 路径混合批语义同形）。

resume 重入 SHALL 以确定性 call_id（`orch_{entry}_{step_idx}_{sid}_{idx}`）为坐标重放，扫描区间 SHALL 限定**本 turn**（history 最后一条 `user_message` 之后，含 gap 回填；无锚点不重放）：区间内已配对的子直接复用 output 不重派发，已完成段零派发跳过；历史轮次的同 call_id 配对 SHALL NOT 命中——call_id 不含 turn 维度，越界命中会使同 thread 第二条 UserMessage 整轮零派发复读旧答案（orch-replay-turn-scope）；when 段判定与 upstream 注入由重放输出重建；`tool_batch_dispatched.count` SHALL 仅计实际派发数（重放命中率可观测）。上层链路（detached spawn 的编排 entry / call_skill 子链）SHALL 复用既有挂起路由，零新增机制。

已知退化：压缩吃掉已完成段的配对 → 重放找不到坐标 → 该子重派发（幂等性由子 skill 自身语义决定）；编排 turn 不采样 LLM、history 短，实际触发概率低。

#### Scenario: 子挂起编排挂起
- **WHEN** serial 段子 skill 触发 request_user_input 挂起
- **THEN** 编排 turn 落 SuspensionRecord（根 pending 为 CHILD_SKILL），该 call_id 仅有悬空 fc，后续段未执行

#### Scenario: 重入零派发重放
- **WHEN** 两段编排在第二段挂起，Resume(leaf) 后重入
- **THEN** 第一段 call_id 命中重放、`tool_batch_dispatched.count == 0`，第二段续跑至编排完成

#### Scenario: 第二条 UserMessage 全量重新派发
- **WHEN** 编排 entry 正常完成第一轮后，同 thread 提交第二条不同输入
- **THEN** 第二轮全部子重新派发（count 为全量），第一轮 fco 不被复用

#### Scenario: when 判定重放一致
- **WHEN** when 段 then 分支挂起后 Resume 重入
- **THEN** 条件 flag 由重放的前序输出重建，then/else 选择与挂起前一致，else 段不执行

## 扩展边界与后路

本节固化「声明式编排到此为止」的边界，避免每次提案重新争论（被 `architecture/skill-system.md` 的 orchestration 节引用）。

### 当前结构：series-parallel，不是任意 DAG

`steps` 是**有序列表**，段间天然串行（barrier）、段内表达并发，顺序由列表位置定义。这是 DAG 的确定性子集（线性 fork-join），结构上不可能有环，故无需在编排层重复环检测。

由此带来的表达力边界，三条都是**有意为之**：
- 无 `depends_on`：不能表达「D 只等 A 和 C，不等 B」
- 数据流只有 `upstream`（= 上一步全部 child 输出）：不能挑特定上游节点
- `when` 限单层：`then` / `else` 内不可再嵌 when

### 不做：任意 DAG（`depends_on` + 拓扑排序）

判 userspace（ADR 0017 规则③），理由按序：

1. **机制不缺，缺的是声明语法**。内核已能表达任意 DAG——节点 = `spawn_skill`，边 = `set_join_barrier(handle_ids, then_skill)`，扇入是 barrier 的天然语义。`depends_on` 只是把「LLM 运行时接线」换成「人在 frontmatter 静态接线」，不填补任何内核机制缺口（规则①不命中）。
2. **静态依赖图 + 重试 + 补跑 = workflow 引擎的领地**。协议边界已开出去：业务侧在内核外驱动 `engine.spawn_skill` / `set_join_barrier` 即可。
3. **横向无拉动**。CLI-agent 阵营（codex / claw-code / openclaw / opencode / hermes-agent / deepagents）全量检索 `depends_on|topological|DAG` 零命中；各家多节点编排一律是运行时句柄式（codex `spawn_agent` / `wait_agent` / `send_message` / `followup_task`），deepagents 虽骑在 LangGraph 上也只对外暴露 subagents + todos，不给用户声明 DAG。

ADR 0006 当年把「流程编排 (DAG)」判为推迟到 M6+；本节将其口径升级为**判 userspace**，非仅推迟。

### 不做：`loop` / `while` 循环原语

同样判不做，理由按序：

1. **引擎本身就是那个循环**。turn 内 LLM↔tool 迭代（`IterationBudget`，cap 默认 32）是循环体；thread 级重入亦已具备——`PeerMailbox` 的 TriggerTurn 可唤醒终态 spawn child 续跑（等价 codex `followup_task` + `resume_agent`）。
2. **模型认知那条路已走完**。模型在循环里缺的不是控制流，是「第几轮了 / 还剩多少预算 / 是否在原地打转」的自知——ADR 0020（budget-awareness-hint）与 ADR 0021（doom-loop 先警后断）已交付。DSL `while` 对模型认知零增量（ADR 0017 规则②不命中）。
3. **代价打在地基上**。当前加载期 `detect_cycles` fail-fast + `CallStack` 环检测换来深度有界、cache anchor 确定、resume 谱系可重建。声明式回边需要给每条边配 trip counter + 计数持久化 + resume 重建，动的是 R2 / R5。

参照实现同样无人做 DSL 循环：codex 把「循环」交给一个专职 prompt 角色（`core/src/agent/builtins/awaiter.toml`，指数退避轮询）；hermes-agent 的 MoA 多层迭代至今标注 "future enhancement"。

### 唯一保留的加法式后路：`upstream_from`

若出现真实业务拉动，扩展 SHALL 走加法式最小切片，且 SHALL NOT 引入任意边或拓扑排序：

```yaml
orchestration:
  steps:
    - parallel: [a, b]
      id: fanout                 # 新增：给 step 命名（可选）
    - serial: [c]
    - serial: [summarize]
      upstream_from: [fanout]    # 新增：默认 = 上一步（省略即旧语义，零行为变更）
```

这解决的是**输入寻址**（挑特定上游输出），不是图结构：结构仍是线性 fork-join，仍不可能有环。改动面限于 `skill/orchestration.py`（parse + 校验）与 `loop/orchestration_exec.py`（按 step id 取 outputs）。

按 ADR 0017 的辅助判据「宿主业务方是否真会用到」——**当前无拉动，不开 change**。
