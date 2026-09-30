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

resume 持写者锁后 strict 读取 Journal，**可收敛 thread** 上满足以下任一条件的调用参与收敛。可收敛 thread =
root thread + 被中断的同步 `call_skill` 派发的子 thread（父 thread 可收敛时子 thread 才可收敛，逐层传递，
见下文「沿 skill 派发树收敛」）：

- **悬空 intent**：`tool_intent_committed` 没有 `tool_outcome_committed`，也没有 `tool_recovery_committed`
  （崩溃在执行途中，模型看不到任何结果）；
- **durable unknown outcome**：`tool_outcome_committed.status == "unknown"` 且未被恢复记录改判（取消 / 超时判不清，
  模型已看到一条错误结果，Session 当时即冻结）。

- **从未登记意图**：`function_call` 会话项已随模型回复 durable，之后同 thread 上没有同 call id 的
  `tool_intent_committed`、`function_call_output` 会话项或 `tool_call_undispatched`（崩溃在模型回复落账与意图
  batch 落账之间）。这类调用的结局是确定的，见下文「从未登记意图的调用」。

不在可收敛 thread 上的调用、引用缺失的 outcome、缺 operation lineage 的 intent 不参与，连同 LLM attempt /
submission 未结算项、无法归属到悬空 `call_skill` 调用的 skill 派发一律 fail closed。`call_skill` 自身的悬空意图
不走下表，按派发谱系结算。

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
| `basis` / `verdict` | 依据 ∈ {reconcile, effect_kind, operator, dispatch}；结论与依据的合法组合见「分流」与「沿 skill 派发树收敛」两表，错配即拒 |
| `reconcile_status?` | 回查原始结论 ∈ {completed, not_executed, unknown, failed}；无回查函数为 null |
| `output?` / `is_error?` | 补写给模型的内容；改判已有 outcome 时二者皆 null |
| `recovery_operation_id` | 接管 operation id（与 `writer_takeover` 同源） |

补写的 `function_call_output` 会话项 record id 为 `{tool operation}:conversation_item:none:1`（与 live outcome 的
ordinal 0 永不相撞），item id 由 (thread, call_id) 确定性派生，metadata 带 `recovered: true`，Responses 调用另带
`origin_llm_sample_id`。

### 从未登记意图的调用（ADR 0075）

strict audit 下整批意图先于任何派发原子落账，所以**没有意图即没有执行**。这类调用不回查、不看副作用声明、
不征求人裁决，恢复时直接落结论：

- 识别按 Journal seq 顺序配对：`function_call` 之后出现的同 call id 的意图、结果会话项或既有结论都会结算它；
  同一 thread 内 call id 被复用时先发出的调用先被结算。
- 结论记录 `tool_call_undispatched`，record id `{tool operation}:tool_call_undispatched:none:0`，挂在该调用**本应
  使用**的 tool operation 下；`causation_id` = `function_call` 会话项 record，`correlation_id` = 接管 operation id，
  actor 为 `system/recovery`。payload `ToolCallUndispatchedV1`：

| 字段 | 说明 |
| --- | --- |
| `function_call_record_id` | 该调用的 `function_call` 会话项 record |
| `call_id` / `name` / `arguments_raw` | 模型发出的调用，取自会话项，原样保留 |
| `output` | 补写给模型的文本，以 `not_executed:` 开头 |
| `is_error` | 恒为 `true` |
| `recovery_operation_id` | 接管 operation id（与 `writer_takeover` 同源） |

- 同 batch 补写 `function_call_output` 会话项，record id `{tool operation}:conversation_item:none:1`，item id 由
  (thread, call_id, `function_call` record id) 确定性派生，metadata 带 `recovered: true`，Responses 调用另带
  `origin_llm_sample_id`。
- 与「结果未知」的结论同属一个恢复 batch，遵守同一条全有或全无规则。
- call id 为空或含 `:`、无法构成 operation identity 的调用无法自动收敛：只读预检即以
  `audit_resume_recovery_required` 拒绝并列出该 `function_call` record，不写接管记录。
- 处置结论以 `not_dispatched` 随 `thread_resumed.recovered_tool_calls` 透出。

#### Scenario: 崩溃在模型回复与意图落账之间
- **GIVEN** 审计会话里模型回复含两个工具调用，回复已 durable，意图 batch 落账前进程死亡
- **WHEN** 另一进程 resume
- **THEN** Journal 原子追加两条 `tool_call_undispatched` 与两条补写的 `function_call_output` 会话项，顺序与模型发出
  调用的顺序一致，strict verify 为 HEALTHY；工具 handler 未被调用
- **AND** 续跑的新 turn 请求里两个调用都带着 `not_executed:` 结果，配对完整

#### Scenario: 已收敛的调用不重复处理
- **GIVEN** 上述 resume 之后进程再次死亡
- **WHEN** 再次 resume
- **THEN** 不再追加 `tool_call_undispatched`，history 中每个调用仍只有一条结果

### 沿 skill 派发树收敛（ADR 0076）

同步 `call_skill` 的子 skill 在独立子 thread 上运行。进程死在子 skill 执行途中时，Journal 留下一条链：父 thread
悬空的 `call_skill` 意图 → 未结算的 `skill_selected` → 子 thread 上待收敛的调用（可以更深）。恢复从 root thread 起
深度优先、自底向上收敛，全部结论同属一个恢复 batch，记录按因果顺序排列（子调用 → 派发终态 → 父调用）。

悬空 `call_skill` 意图的结局只由已 durable 的派发谱系决定，不看其 `effect_kind` 声明、不回查、不征求人裁决：

| 谱系形态 | `tool_recovery_committed` | 补写的派发记录 | 补写给模型的 `function_call_output` |
| --- | --- | --- | --- |
| 没有 `skill_selected` | `dispatch` / `not_started` | 无 | `not_executed: ...`（`is_error=true`） |
| 有 selected，无 started、无 finished | `dispatch` / `not_started` | `skill_dispatch_finished(rejected, process_recovery_before_start)`，不带子谱系 | 同上 |
| 有 finished | `dispatch` / `completed` | 无 | 成功：已落账的 `final_text`；否则 `sub_skill_failed: <end_reason>`（`is_error=true`） |
| 有 started，无 finished | `dispatch` / `interrupted` | `skill_dispatch_finished(cancelled, process_recovery)` + 子 thread `thread_terminal(cancelled, process_recovery)` | `skill_dispatch_interrupted: ...` + 子 skill 内各调用的处置清单（`is_error=true`） |

- 被中断的派发先收敛其子 thread：结果未知的调用按上文「分流」、从未登记意图的调用按上文规则、嵌套的
  `call_skill` 递归处理。子 thread 上任一调用仍需人裁决时，该派发与其父调用都不落结论，整批不写。
- 补写的派发记录与 live 路径同 record id（`{skill operation}:skill_dispatch_finished:none:0` /
  `{skill operation}:thread_terminal:none:0`），actor 为 `system/recovery`，`correlation_id` = 接管 operation id，
  `stable_error.code = "skill_dispatch_interrupted"`（`retryable=true`）。
- 被中断的执行**不记战绩**：不写 `skill_outcome` 会话项。执行没有跑完，不是 skill 的成败。
- 父调用结果里的处置清单只列**直接子层**的调用（`<工具名> (<call_id>): <处置>`）；更深层的结论 durable 在
  Journal 各自的 thread 上。
- 交人裁决的请求 `AuditToolOutcomeRequest.thread_id` 为该调用所在的 thread（子 thread 的调用即子 thread id）。
- 恢复不续跑子 skill、不执行工具。需要重做由模型在续跑的 turn 里重新派发。
- `thread_resumed.recovered_tool_calls` 只列 root thread 上的调用；`call_skill` 的处置为 `not_dispatched` /
  `reconciled` / `dispatch_interrupted`。
- resume 对恢复写过记录的子 thread 同样核对 transcript 投影（缺后缀补齐，分叉只标 stale）。

#### Scenario: 子 skill 的工具执行途中崩溃
- **GIVEN** 审计会话里入口 skill 经 `call_skill` 派发子 skill，子 skill 的 `remote_write`（提供 `reconcile`）执行途中
  进程死亡
- **WHEN** 另一进程 resume，回查返回 `completed`
- **THEN** Journal 原子追加：子 thread 上的 `tool_recovery_committed(reconcile/completed)` + 结果会话项 →
  `skill_dispatch_finished(cancelled)` + 子 thread `thread_terminal` → 父 thread 上的
  `tool_recovery_committed(dispatch/interrupted)` + 结果会话项；strict verify 为 HEALTHY
- **AND** root history 末项是 `call_skill` 的结果，正文含 `remote_write (...): reconciled`

#### Scenario: 子 thread 的调用只能交人而无人可问
- **GIVEN** 上述崩溃，但 `remote_write` 非幂等、无回查，且未配置 resolver
- **WHEN** resume
- **THEN** 只读预检即以 `audit_resume_recovery_required` 拒绝，`record_ids` 只含子 thread 的那条意图，Journal 无新增

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
