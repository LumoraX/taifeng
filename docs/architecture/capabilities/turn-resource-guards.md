# Capability: turn-resource-guards

## Purpose

turn 级资源护栏的两条正交契约（token 维 K2 / 广度维 K1 之外的空洞）：

1. **denial 断路器**：同 turn 内 permission/hook 连续拒绝越阈值 → 单次触发、迭代边界提前终止（`end_reason="denial_circuit_open"`），替代「被拒后在 max_iterations 内空转重试白烧 token」。参照 codex `guardian/mod.rs` `GuardianRejectionCircuitBreaker`。
2. **迭代预算分层 + 退还**：裸 `while iterations < max_iterations` 计数器抽成 `IterationBudget`（consume/refund/child），子 turn 派生独立预算（父子总和可超父 cap——hermes 有意语义）、内置工具可静态声明 refund。参照 hermes `iteration_budget.py`。

第三轮对比分析 P1 缺口 C1+C2。实现：`src/taifeng/loop/denial_breaker.py`、`src/taifeng/loop/iteration_budget.py`、`loop/turn.py`（循环重构 + `_note_tool_outcome` 单点记账）、`tool/spec.py`（`refunds_iteration`）、`tool/runtime.py`（`spec_for`）、`loop/event.py`（`DenialCircuitOpen`）。

## 数据契约

### `DenialBreakerConfig`（frozen，业务注入；两阈值均 None = 永不触发）

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `max_consecutive_denials` | None | 连续 deny 触发阈值（任一成功重置） |
| `max_recent_denials` | None | 滑窗内 deny 数触发阈值 |
| `window_size` | 16 | 滑窗大小 |

注入路径：`EnginePool.create(denial_breaker_config=…)` / `AgentEngine(denial_breaker_config=…)` → 每 TurnRunner **每 turn 新建** `DenialBreaker` 实例（turn 级生命周期，无跨 turn 状态，R5 ⚪；不放进无状态共享的 `PermissionPolicy`）。

### `IterationBudget`

`consume() -> bool`（耗尽 False、不计数）/ `refund(n)`（clamp 防负）/ `spent` / `remaining` / `cap` / `child(cap=None)`（独立实例，默认 cap=父**初始** cap 非剩余）。`TurnOutcome.iterations` 与 `turn_completed.data["iterations"]` = 净消费（spent；refund 后回落）。圈序号（rewind 节点 / `SuspensionRecord.turn_index`）由独立单调计数器承担，不受 refund 影响。

### `ToolSpec.refunds_iteration: bool = False`

dispatch **成功**完成（非 error、非挂起）且标记为 True → 外层预算 `refund(1)`；失败轮照常计费。仅 spec 静态声明 + 内核 dispatch 路径生效，不暴露为 LLM 可触发语义。**内核不为任何既有内置工具默认开启**（使用方决策）。

### `DenialCircuitOpen` 事件（`kind="denial_circuit_open"`）

`data = {consecutive, recent, window_size, last_denied_target}`（target 仅名字，不带 args 正文）。console sink 专用渲染（`perm ⊘` 红）。

## 行为契约

### Requirement: 单点记账与触发
- deny 判定 = 工具配对回填处统一观察 `ToolResult.data["reason"] ∈ {"hook_denied", "permission_denied"}`（含 HITL ask 超时产生的 deny）；成功结果重置 consecutive；其他 error 中性。consecutive 或滑窗任一越阈值 → `DenialCircuitOpen` emit **恰好一次**（闩锁），turn 在迭代边界以 `end_reason="denial_circuit_open"` 终止——当轮 fc/output 已配对落史，无孤儿（K5 一致）。本圈无后续 tool call 时自然 `completed` 优先（断路只阻后续空转）。

### Requirement: 预算行为等价与分层
- 默认（不注入 budget/config、不开 refund）行为与裸计数器逐项等价（全量回归零断言改动守护）；取消检查仍在 consume 之前（取消圈不计费，与原语义一致）。
- `run_sub_skill` 派生子 runner 传 `budget.child()`：子消费不回写父、父子总和可超父 cap；detached spawn / resume 续跑等其余 TurnRunner 构造点各自新建默认预算。

### Requirement: 配置注入（R1）
- 阈值/上限全部构造期注入；断路器只消费 deny 结果、不改 `PermissionPolicy` 裁决路径。

### Requirement: 触顶经失败处置 policy 判定（failure-suspension-policy）
- 三类护栏触顶（`max_iterations` / `resource_limit_exceeded` / `denial_circuit_open`）在终结 turn 前经注入的 `FailureDispositionPolicy` 判定（`origin="guard_trip"`）：TERMINAL → 既有 end_reason 终结路径（默认 policy 恒 TERMINAL，零行为变化）；SUSPEND → 改落 `RESOURCE_LIMIT` 挂起（detail 携带 `end_reason` + 护栏快照，断路触发时 `denial_circuit_open` 事件仍恰好一次）。retry 续跑时预算与断路器随 runner 重建按原 cap 重置。完整契约见 [suspend-resume.md](suspend-resume.md) §失败处置裁决 policy。

### Requirement: limit 类失败 retry 语义（resource-limit-retry-semantics）
- **K2 retry = 预算增额裁决**：`limit_kind="session_tokens"` 触顶挂起的 retry payload 必须携带 `{"action":"retry","extend_tokens":N>0}`（engine 抬升 `_max_session_tokens`,触顶条件随之清除）;裸 retry / 非法增额 → `ResolveError`（触顶条件跨 turn 单调递增,裸 retry 必然立即再触顶 = 无效裁决,禁 silent 循环）。该挂起 `on_expire` 恒 abort（覆写配置——自动 retry 无人携带增额必然无效）。
- **Resume 续跑路径过 K2 闸门**：与 UserMessage 路径同判;会话已触顶的续跑不得静默烧 token,按 policy 再裁决（挂起 / 终态）。
- **limit 类失败全面进 policy**：K2 引擎级拒新 turn（policy SUSPEND → engine 级 RESOURCE_LIMIT 挂起,user_message 已入史,retry+增额后该 turn 正常执行）与 RequestTooLargeError 预检（SUSPEND → SYSTEM_RETRY 挂起,业务 CompactNow / 改参后 retry 可过）均咨询 policy;Conservative 对两者恒 TERMINAL,零行为变化。
- **自动 retry 有界**：pending detail 携带 `auto_retry_count` 谱系计数（TTL 到期自动 retry 续跑 +1;人工 Resume 恒 0）;达 `failure_suspend_max_auto_retries`（None=不限）后到期裁决强制 abort,`suspension_expired.data` 标注 `auto_retry_exhausted: true`——熔断无人值守无界循环。
- **观测如实（R3）**：护栏触顶经 policy 裁决 SUSPEND 时 `ResourceLimitExceeded.scope` 为 `"turn_suspended"`（turn 并未 abort）;TERMINAL 时维持 `"turn_aborted"` / `"turn_refused"`。

## R1–R5 影响

- **R1**：✅ 计数与记账无业务语义；阈值业务注入。
- **R2**：⚪ 不触压缩/cache。
- **R3**：✅ `denial_circuit_open` 事件 + console 专用渲染；`end_reason` 经既有 turn 终结事件透出。
- **R4**：✅ 断路终止走既有迭代边界终结路径（配对安全）；取消语义与原循环逐项一致。
- **R5**：⚪ 两护栏均 turn 内瞬态，无新增持久态。

## 测试

`tests/loop/test_iteration_budget.py`（5：耗尽/refund clamp/子独立/显式子 cap）、`tests/loop/test_denial_breaker.py`（6：连续/重置/滑窗/驱逐/无阈值/snapshot）、`tests/loop/test_turn_resource_guards.py`（3 e2e：连续 deny 断路恰好一次 + end_reason；refunds 工具 5 轮跑过 cap=3 净耗 1；父 cap=3 耗 1 后子独立跑满 3 圈）。行为等价由全量回归（零断言改动）守护。`tests/loop/test_resource_limit.py`（K2 触顶挂起 retry 增额闭环 / turn_refused 进 policy / RequestTooLarge 预检双 policy）、`tests/test_suspension_ttl.py::test_auto_retry_lineage_exhaustion_forces_abort`（谱系熔断）。
