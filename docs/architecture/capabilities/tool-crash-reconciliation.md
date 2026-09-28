# Capability: tool-crash-reconciliation

## Purpose

进程在工具**执行途中**崩溃时，冷恢复要能识别「这次调用到底发生没有」，并按工具声明的副作用
类型分流处置——而不是让模型在不知情的情况下重发一遍（副作用静默重复），也不是一刀切标「未知」。

修复的缺口（2026-09-28 review 核实）：

- Chat 协议路径的 `function_call` 要等工具**执行完**才与 output 成对落盘；执行期间崩溃，调用意图
  整个丢失，resume 后模型重发 → 非幂等副作用（转账 / 发信 / 建单）静默重复。
- Responses 路径的悬空调用一律回填「结果未知，不重试」，不区分幂等工具与可回查工具。
- `ToolSpec` 已声明 `effect_kind` / `reconciliation`（ADR 0025），但恢复时没有任何代码读它们。

参照：ADR 0025 恢复语义（未匹配 intent 一律 UNKNOWN，仅幂等或 reconciler 证明后才重试 / 补写）；
claw-code recovery recipe。决策：ADR 0045。

实现：`conversation/models.py`（`tool_intent` kind / `BOOKKEEPING_ITEM_KINDS`）、
`loop/turn_sample.py`（派发前写意图）、`loop/tool_recovery.py`（识别 + 分流 + 落盘）、
`loop/pool_session.py`（legacy resume 接线）、`tool/spec.py`（`reconcile` / `ReconcileVerdict`）、
`suspend/reason.py` + `suspend/resolver.py`（`TOOL_OUTCOME_UNKNOWN`）。

## 数据契约

### `tool_intent` ResponseItem（记账类，不进 LLM 视图）

`payload = {"call_id", "name", "arguments", "extra_content"?}`。Chat 协议路径（非 strict audit、
非 Responses）在一批工具**派发前**逐条落盘，**同时进 hot history**（保证 hot 与 store 逐项一致，
rewind 的 `cut_index` 下标不漂移）。prompt 渲染跳过；token 估算计 0。

`BOOKKEEPING_ITEM_KINDS = {suspension, spawn, join_barrier, join_barrier_fired, skill_outcome,
spawn_settled, tool_intent}`——这些 kind 估算 token 计 0（此前按 50 计，属高估）。

### `ToolSpec.reconcile: ReconcileFunc | None`

`ReconcileFunc = (arguments: dict, call_id: str) -> Awaitable[ReconcileVerdict]`。

| `ReconcileVerdict.status` | 含义 | 恢复处置 |
| --- | --- | --- |
| `completed` | 副作用已发生 | 以 `output` / `is_error` 回填真实结果 |
| `not_executed` | 确认未执行 | 回填「未执行，可安全重发」（is_error） |
| `unknown` | 查不清 | 交人裁决 |

回查受 `ToolSpec.timeout_seconds` 限时；参数非法 / 抛异常 / 超时均视同 `unknown`（有日志）。

### `SuspendReason.TOOL_OUTCOME_UNKNOWN`

交人裁决的挂起。`related_call_id` = 悬空调用；`detail = {tool, arguments, effect_kind,
reconciliation, registered}`。resolutions payload（必须是 dict）：

| payload | 续跑动作 |
| --- | --- |
| `{"action": "retry"}` | 内核重新执行该调用（复用 permission allow 的执行路径） |
| `{"action": "provide", "output": str, "is_error"?: bool}` | 原样回填人给出的真实结局（保留 is_error） |
| `{"action": "abort"}` | 回填 `tool_outcome_unknown: aborted ...` 并终止续跑 |

非法 action / provide 缺 output → `ResolveError`。TTL 到期只能 abort（构造期禁 `on_expire="retry"`），
回填文案前缀 `tool_outcome_unknown:`——不暗示「确定没执行」。

### `EnginePool(tool_recovery="suspend" | "report")`

非幂等且无法回查时的处置：`suspend`（默认）挂起交人；`report` 回填
`tool outcome unknown after process recovery; not retried`（旧行为）。非法值构造期 `ValueError`。

### `thread_resumed` 事件

新增 `recovered_tool_calls: [{call_id, name, disposition}]`，`disposition ∈ {safe_to_retry,
reconciled, awaiting_operator, reported_unknown}`；`recovered_unknown_call_ids` 保留为
`awaiting_operator ∪ reported_unknown` 的 call_id。

## 行为契约

### Requirement: 悬空调用识别

冷加载（legacy resume，`reconstruct_logical_history` 之后）SHALL 把满足以下条件的调用视为悬空：
出现过 `tool_intent` 或 `function_call`，没有同 call_id 的 `function_call_output`，且不被未核销的
挂起记录持有（`related_call_id`）。strict audit 会话不走本路径（Journal 自有 UNKNOWN 语义）。

### Requirement: 分流

| 条件（按序判定） | disposition | 落盘 |
| --- | --- | --- |
| 有 `reconcile` 且回查 `completed` | `reconciled` | 真实结果 output |
| 有 `reconcile` 且回查 `not_executed` | `safe_to_retry` | 「未执行，可安全重发」 |
| 无 `reconcile` 且 `effect_kind ∈ {pure, idempotent}` | `safe_to_retry` | 「中断，工具声明为 X，可安全重发」 |
| 其余（含未注册工具、回查 `unknown` / 失败），`tool_recovery="suspend"` | `awaiting_operator` | 一条合并的 `TOOL_OUTCOME_UNKNOWN` 挂起记录 |
| 同上，`tool_recovery="report"` | `reported_unknown` | 「结果未知，不重试」 |

恢复**从不自动执行工具**；「可安全重发」由模型自行决定是否重调。

### Requirement: 落盘顺序与幂等

1. 只有意图的 Chat 调用先补 `function_call`（同参，确定性 id）；
2. 回填 output：Chat 逐条追加；Responses 按采样原子批（`batch_id = recovery:unknown_tool_outcome:<digest>`，
   output 带 `origin_llm_sample_id`）；
3. 需人裁决的调用合并为一条挂起记录（`submission_id="recovery"`，id 确定性派生）。

全部补写项 id 由 (thread / sample, call_id) 派生；恢复途中再崩溃，下次恢复 SHALL NOT 重复补写。

#### Scenario: 非幂等工具崩溃后交人
- **WHEN** Chat 路径 `transfer`（`external_non_idempotent`）执行途中进程崩溃，随后 resume
- **THEN** history 补上 `function_call`，出现一条 `TOOL_OUTCOME_UNKNOWN` 活跃挂起；新用户消息被活跃挂起守卫拒绝
- **AND** 提交 `Resume(provide)` 后回填人给出的结果并续跑完成

### Requirement: 热路径写前日志

Chat 路径 SHALL 在调用 `dispatch_batch` 之前把本批全部意图 `store.append` 完成；工具 handler
运行时 store 中已可见其意图。

## R1–R5 影响

- R1：副作用分类是 ADR 0025 既有的内核元数据；回查逻辑由业务注入。
- R2：`tool_intent` 不进 prompt，不影响 cache 前缀。
- R3：处置结论随 `thread_resumed` 透出；交人裁决走既有 `turn_suspended` / `suspension_*` 事件族。
- R4：回查受工具超时约束。
- R5：本能力即 R5 在副作用层面的补全——崩溃后可以正确 resume。
