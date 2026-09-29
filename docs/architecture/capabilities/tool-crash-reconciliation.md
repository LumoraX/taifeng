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
claw-code recovery recipe。决策：ADR 0045（非审计路径）、ADR 0070（strict audit 路径）。

实现：`conversation/models.py`（`tool_intent` kind / `BOOKKEEPING_ITEM_KINDS`）、
`loop/turn_sample.py`（派发前写意图）、`loop/tool_recovery.py`（识别 + 分流 + 落盘，并导出两条路径共用的
分流原语 `RETRY_SAFE_EFFECTS` / `run_reconcile` / 回填文案 / `stable_recovery_id`）、
`loop/pool_session.py`（resume 接线）、`tool/spec.py`（`reconcile` / `ReconcileVerdict`）、
`suspend/reason.py` + `suspend/resolver.py`（`TOOL_OUTCOME_UNKNOWN`）；strict audit 路径见文末
`loop/audit_resume_tools.py`（识别 + 分流 + 落账记录）、`loop/audit_resume_resolution.py`（人裁决 DTO /
resolver 类型）、`conversation/journal/recovery_records.py`（`ToolRecoveryCommittedV1`）。

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
reconciled, awaiting_operator, reported_unknown, operator_resolved}`（`operator_resolved` 只出现在 strict audit
resume）；`recovered_unknown_call_ids` 保留为 `awaiting_operator ∪ reported_unknown` 的 call_id。

## 行为契约

### Requirement: 悬空调用识别

冷加载（legacy resume，`reconstruct_logical_history` 之后）SHALL 把满足以下条件的调用视为悬空：
出现过 `tool_intent` 或 `function_call`，没有同 call_id 的 `function_call_output`，且不被未核销的
挂起记录持有（`related_call_id`）。strict audit 会话不走本路径，而走下文「strict audit 路径」（Journal 自有
UNKNOWN 语义与落账方式）。

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

## strict audit 路径（ADR 0070）

审计会话的 transcript 只是投影，事实在 SessionJournal；恢复结论因此写成新的 Journal 记录，而不是回填 transcript。

### 待收敛的调用

resume 持写者锁后 strict 读取 Journal，root thread 上满足以下任一条件的调用参与收敛：

- **悬空 intent**：`tool_intent_committed` 没有 `tool_outcome_committed`，也没有 `tool_recovery_committed`
  （崩溃在执行途中，模型看不到任何结果）；
- **durable unknown outcome**：`tool_outcome_committed.status == "unknown"` 且未被恢复记录改判（取消 / 超时判不清，
  模型已看到一条错误结果，Session 当时即冻结）。

子 thread 的调用、引用缺失的 outcome、缺 operation lineage 的 intent 不参与，连同 LLM / skill / submission 未结算项
一律 fail closed。

### 分流

| 条件（按序判定） | basis / verdict | 随 batch 补写的 `function_call_output` |
| --- | --- | --- |
| 有 `reconcile`，回查 `completed`，悬空 intent | `reconcile` / `completed` | 真实结果（保留 `is_error`） |
| 有 `reconcile`，回查 `not_executed` | `reconcile` / `not_executed` | 悬空：「未执行，可安全重发」；已有结果：不补 |
| 无 `reconcile`，悬空 intent，intent 落账的 `effect_kind` 与当前注册声明**都**属 pure / idempotent | `effect_kind` / `retry_safe` | 「中断，工具声明为 X，可安全重发」 |
| 其余，`AuditConfig.tool_outcome_resolver` 给出 `provide`（仅悬空 intent） | `operator` / `provided` | 人给出的结果（保留 `is_error`） |
| 其余，resolver 给出 `abort` | `operator` / `aborted` | 悬空：`tool_outcome_unknown: aborted by operator ...`；已有结果：不补 |
| 其余（含回查 `unknown` / 失败、已有结果却回查 `completed`、未注册工具、无 resolver 或 resolver 返回 None） | — | 不写；resume 拒绝 |

回查受 `ToolSpec.timeout_seconds` 限时；抛异常 / 超时 / 返回值不是 `ReconcileVerdict` 均视同查不清（有日志）。
已有 durable 结果的调用不能补第二条 output（破坏配对且违反 append-only），所以只接受「未执行」或人接受未知。
恢复**从不执行工具**，也不接受 `retry` 裁决（审计模式的 effect 只能发生在有 durable intent 的 turn 内）。

### `tool_recovery_committed` 记录

record id `{tool operation}:tool_recovery_committed:none:0`，挂在原 intent 的 tool operation 下；`causation_id` =
被结算的 record（intent 或 unknown outcome），`correlation_id` = 本次接管的 operation id。actor：自动结论为
`system/recovery`，人裁决为 `operator/recovery` + `principal_id=operator_id`。payload `ToolRecoveryCommittedV1`：

| 字段 | 说明 |
| --- | --- |
| `intent_record_id` / `outcome_record_id?` | 被结算的 intent；改判 unknown outcome 时指向该 outcome |
| `call_id` / `name` / `effect_kind` | 调用标识与 intent 落账时的副作用声明 |
| `basis` / `verdict` | 依据 ∈ {reconcile, effect_kind, operator}；结论与依据的合法组合见上表，错配即拒 |
| `reconcile_status?` | 回查原始结论 ∈ {completed, not_executed, unknown, failed}；无回查函数为 null |
| `output?` / `is_error?` | 补写给模型的内容；改判已有 outcome 时二者皆 null |
| `recovery_operation_id` | 接管 operation id（与 `writer_takeover` 同源） |

补写的 `function_call_output` 会话项 record id 为 `{tool operation}:conversation_item:none:1`（与 live outcome 的
ordinal 0 永不相撞），item id 由 (thread, call_id) 确定性派生，metadata 带 `recovered: true`，Responses 调用另带
`origin_llm_sample_id`。

### 行为契约

- 回查与 resolver 只在持写者锁之后调用；只读预检把「无回查、不可安全重发、又无 resolver」的调用直接拒绝，不写接管记录。
- **全有或全无**：全部调用都得出结论才把恢复记录作为一个 batch 原子追加（接管 lease、`expected_seq` = 接管 ack），
  追加后 strict 重读确认已无未结算 effect；任一调用仍需人裁决即一条不写，`AuditResumeError("audit_resume_recovery_required")`
  只列出需要人处置的 record。
- resolver 返回类型不对，或对已有结果的调用 `provide` → `audit_resume_resolution_invalid`（`record_ids` 为该 record）；
  resolver 自身异常原样上抛；两者都先释放 lease、不写恢复记录。
- 恢复记录一经 durable 即为该 intent 的终态：再次崩溃后的 resume 冷读它、不再重复收敛。
- `EnginePool(tool_recovery=)` 不作用于审计模式：审计从不自动回填「结果未知」再续跑（ADR 0053 §4）。

#### Scenario: 可回查工具崩溃后审计 resume
- **WHEN** 审计会话里 `remote_write`（`reconcilable` / `query`，提供 `reconcile`）执行途中进程死亡，随后另一进程 resume
- **THEN** Journal 在 epoch 2 的 `writer_takeover` 之后原子追加 `tool_recovery_committed(reconcile/completed)` 与补写的
  `function_call_output` 会话项，strict verify 为 HEALTHY
- **AND** 续跑的新 turn 请求里带着回查得到的真实结果

## R1–R5 影响

- R1：副作用分类是 ADR 0025 既有的内核元数据；回查逻辑由业务注入。
- R2：`tool_intent` 不进 prompt，不影响 cache 前缀。
- R3：处置结论随 `thread_resumed` 透出；交人裁决走既有 `turn_suspended` / `suspension_*` 事件族（审计路径：
  拒绝时 `AuditResumeError.record_ids`，收敛结论 durable 在 Journal）。
- R4：回查受工具超时约束。
- R5：本能力即 R5 在副作用层面的补全——崩溃后可以正确 resume。
