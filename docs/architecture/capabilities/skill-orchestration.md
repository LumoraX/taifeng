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

resume 重入 SHALL 以确定性 call_id（`orch_{entry}_{step_idx}_{sid}_{idx}`）为坐标重放：history 中已配对（含 gap 回填）的子直接复用 output 不重派发，已完成段零派发跳过；when 段判定与 upstream 注入由重放输出重建；`tool_batch_dispatched.count` SHALL 仅计实际派发数（重放命中率可观测）。上层链路（detached spawn 的编排 entry / call_skill 子链）SHALL 复用既有挂起路由，零新增机制。

已知退化：压缩吃掉已完成段的配对 → 重放找不到坐标 → 该子重派发（幂等性由子 skill 自身语义决定）；编排 turn 不采样 LLM、history 短，实际触发概率低。

#### Scenario: 子挂起编排挂起
- **WHEN** serial 段子 skill 触发 request_user_input 挂起
- **THEN** 编排 turn 落 SuspensionRecord（根 pending 为 CHILD_SKILL），该 call_id 仅有悬空 fc，后续段未执行

#### Scenario: 重入零派发重放
- **WHEN** 两段编排在第二段挂起，Resume(leaf) 后重入
- **THEN** 第一段 call_id 命中重放、`tool_batch_dispatched.count == 0`，第二段续跑至编排完成

#### Scenario: when 判定重放一致
- **WHEN** when 段 then 分支挂起后 Resume 重入
- **THEN** 条件 flag 由重放的前序输出重建，then/else 选择与挂起前一致，else 段不执行
