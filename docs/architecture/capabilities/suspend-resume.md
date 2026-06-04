# suspend-resume Specification

## Purpose

通用**挂起 / resume 原语**（业务无关）：一个 turn 可在任意点产出"挂起"作为**正常结局**（而非阻塞协程驻留内存），随后释放实例；业务侧之后凭 `thread_id` + `resolutions` 提交 `Resume` Op 续跑。覆盖两大类挂起：

- **人类输入类**：权限审批（`permission`）、表单填写（`form`）、外部数据（`data`）。
- **系统态类**：LLM 限流 / 配额 / 余额 / key 鉴权失败 / 可恢复网络错（`system_retry`），自动 retry 耗尽后转挂起。

设计源文档：`docs/superpowers/specs/2026-06-02-suspend-resume-design.md`；决策记录：ADR 0012。

参照 **openclaw 重入模型**（挂起=turn 早返回结局 + 决定存外部 + 重入续跑）+ **codex 协议形状**（typed EventMsg + 关联 id + pending-map 按 id 索引）。差异（参照 X，差异 Y）：codex / hermes / claw-code 都是内存阻塞、不支持进程可退后 resume；taifeng 复用 `function_call` 无 `function_call_output` 的 **history-gap** 表示挂起点，resume 填 output，**不重跑 tool**；codex 每种 ask 一个 Op，taifeng 收敛成**单个通用 `Resume` Op** + typed `reason` 区分；codex **不持久化** turn 中途 pending，taifeng **额外落 `SuspensionRecord`** 标记中途断点，使 mid-turn resume 跨进程可行。

## 数据契约

### Requirement: `SuspendReason` 五值枚举

`SuspendReason` SHALL 是 `enum.StrEnum`，取且仅取以下五值（决定 resume 续跑语义）：

| 值 | 语义 | resume 续跑动作 |
| --- | --- | --- |
| `permission` | 等权限审批 | `granted=true` → resume 时执行该 tool；`granted=false` → 回填 error output |
| `form` | 等用户填表 | payload 直接成该 call 的 `function_call_output` |
| `data` | 等外部数据 | 同 `form`，payload 直接成 `function_call_output` |
| `system_retry` | 限流 / 配额 / 余额 / 鉴权 / 可恢复网络错 | `action=retry`（默认）→ 重跑同次 sample；`action=abort` → turn 终止不续跑 |
| `child_skill` | `call_skill` 派发的子 skill 内部挂起 → 父的 `call_skill` 随之挂起 | **非用户可直接 resolve**：由 engine 续跑链内部核销——先续跑子 thread 拿结果，再回填本 `call_skill` 的 `function_call_output`（见下「子 thread resume 续跑链」） |

`StrEnum` 保证 JSON 序列化为字符串（`reason.value`），跨进程 `from_item` 还原稳定。`child_skill` pending 的 `related_call_id` = 父 `call_skill` 的 call_id，`detail` 携带 `{sub_thread_id, skill_id}`（子 thread + 子 entry skill）。

#### Scenario: 枚举值与 JSON 序列化
- **WHEN** 序列化 `SuspendReason.PERMISSION`
- **THEN** 落盘为字符串 `"permission"`；`SuspendReason("permission")` 可还原

### Requirement: `PendingRequest`（单个挂起点）

`PendingRequest` SHALL 是 `@dataclass(frozen=True)`，字段：

- `request_id: str` —— 关联 id（对标 codex call_id）；`Resume.resolutions` 的 key
- `reason: SuspendReason`
- `payload_schema: dict[str, Any] = {}` —— JSON Schema，业务/前端据此渲染表单或审批 UI
- `related_call_id: str | None = None` —— 关联的 `function_call` call_id；**人类输入类必有**，系统态为 `None`
- `detail: dict[str, Any] = {}` —— 不透明上下文（scope / target / command / failure_class 等）；**taifeng 不解析其 keys（R1）**

### Requirement: `SuspensionRecord` + `suspension` ResponseItem 编码

`SuspensionRecord` SHALL 是 `@dataclass(frozen=True)`，一条 record 对应 turn 的一次挂起，可含**多个** `PendingRequest`（多挂起点并存，共享同一 `record_id`）：

- `record_id: str` —— 幂等键（重复 resume 检测）
- `thread_id: str` / `submission_id: str` / `turn_index: int`
- `pending: tuple[PendingRequest, ...]`
- `created_at: int` —— **业务侧时间戳**（R1：src 内不取系统时钟，由注入工厂传入）

方法：`request_ids() -> set[str]`（resume 校验用）、`to_item() -> ResponseItem`、`from_item(item) -> SuspensionRecord`（`item.kind != "suspension"` 抛 `ValueError`）。

`suspension` ResponseItem 编码（落 JSONL，使 mid-turn 挂起可跨进程 resume — R5）：`kind="suspension"`，`payload = {record_id, submission_id, turn_index, pending: [...], created_at, resolved: False}`，`thread_id` 同 item.thread_id。`pending` 每项展开为 `{request_id, reason(=.value), payload_schema, related_call_id, detail}`。

#### Scenario: round-trip 不丢字段
- **WHEN** `SuspensionRecord.from_item(record.to_item())`
- **THEN** 还原的 record 与原始等价（reason 从字符串还原为枚举；缺省 payload_schema / detail / related_call_id 对齐 dataclass 默认值）

### Requirement: `Resume` Op

`Resume` SHALL 是 pydantic discriminated Op（`kind="resume"`），加入 `loop/submission.py` 的 `Op` Union：

- `thread_id: str` —— 要续跑的 thread
- `resolutions: dict[str, Any]` —— `{request_id: payload}`；**必须一次补齐该挂起 record 的全部 pending**（不允许部分 resume）

payload 形状由对应 `PendingRequest.reason` 决定：

- `permission`：`{"granted": bool, "reason"?: str, "remember_until"?: str}`
- `form` / `data`：任意 JSON（直接成 `function_call_output`）
- `system_retry`：`{"action": "retry" | "abort"}`

### Requirement: `ResolvePlan` + `SuspensionResolver`

`SuspensionResolver`（无状态）SHALL 提供 `validate(record, resolutions)` 与 `plan(record, resolutions) -> ResolvePlan`。`ResolvePlan`（`@dataclass`）字段：

- `execute_tool_call_ids: list[str]` —— permission allow → resume 时执行 tool
- `direct_outputs: dict[str, Any]` —— call_id → output（form / data）
- `deny_outputs: dict[str, str]` —— call_id → deny reason（permission deny）
- `resample: bool` —— system_retry → 重跑 sample
- `abort: bool` —— system_retry `action=abort`

`ResolveError`（普通 `Exception`，**不进 LLMError 体系**）在：resolutions 不全 / 多余、人类输入类 pending 缺 `related_call_id`、未知 reason 时抛出（禁 silent fallback）。

### Requirement: 三个 EventMsg（R3 可观测）

新增三个 `EventMsg.msg` variant：

| kind | 触发 | data 形状 |
| --- | --- | --- |
| `turn_suspended` | turn 挂起的契约事件类型（定义并导出，业务可构造 / 匹配） | `{thread_id, record_id, pending: [{request_id, reason, payload_schema, related_call_id, detail}], cache_invalidated}` |
| `suspension_resolved` | `Resume` 成功配对、turn 续跑 | `{record_id, request_ids: list[str]}` |
| `suspension_resolve_rejected` | `Resume` 被拒（resolution 不全 / 多余、无活跃挂起、ResolveError 等） | `{reason: str, record_id: str \| None, detail: dict}` |

> 当前实现：挂起结局**经独立终结态 `turn_suspended` 在事件流上被观测**（`run_turn` 终结 emit 在 `end_reason == "suspended"` 时发 `TurnSuspended` 而非 `TurnCompleted`，业务侧据此与 `completed` / `cancelled` / `error` 区分）；`TurnOutcome.end_reason` 仍为 `"suspended"`（返回值不变，供 `_handle_resume` 等内部路径判定）。`suspension_resolved` / `suspension_resolve_rejected` 由 `AgentEngine._handle_resume` 直接 emit。

## 行为契约

### Requirement: suspend → release → resume 生命周期

挂起点不再阻塞 `await`，而是抛 `SuspendSignal(PendingRequest)`（内部控制流异常，**不继承 LLMError**）。链路：

```
深处挂起点 raise SuspendSignal
  → dispatch_batch 捕获为 ToolCallOutcome.suspend（不 fail-fast，整批收集）
  → _dispatch_tools 聚合整批 → raise _BatchSuspend(pending...)
  → run_turn: except _BatchSuspend / except SuspendSignal
       → _persist_suspension：落一条 suspension item（history + store）
       → end_reason = "suspended"，TurnOutcome.suspension = SuspensionRecord
  → engine：turn 协程彻底退栈，实例可释放
─────────── 数小时后 ───────────
Resume(thread_id, resolutions)
  → engine._handle_resume：找活跃挂起 → resolver.plan → 应用 plan → 落 resolved-marker
       → emit suspension_resolved → 非 abort 则 _build_and_run_runner 续采样
```

#### Scenario: 挂起 turn 的结局
- **WHEN** turn 命中挂起点
- **THEN** `end_reason == "suspended"`、落一条 `suspension` item、`TurnOutcome.suspension` 非空、turn 协程退栈

### Requirement: 四种 reason 的 resume 语义

- **permission allow**（`granted=true`）：resume 时**真正执行**该挂起 tool（`engine._execute_resumed_tool`，复用 `tool_runtime.dispatch`，走 RwLock），回填 `function_call_output`。执行前调 `PermissionPolicy.preapprove(call_id)` 一次性放行，避免 `SuspendingPrompter` 二次挂起（防无限挂）。
- **permission deny**（`granted=false`）：回填 `is_error=True` 的 `function_call_output`（`permission_denied: <reason>`），让模型据此改写后续。
- **form / data**：`resolutions[request_id]` 直接 JSON 序列化成该 `related_call_id` 的 `function_call_output`（`is_error=False`），**不重跑 tool**。
- **system_retry**：`action=retry`（默认）→ 不动 history，重跑那次 `_sample_once`（获全新 retry 预算）；`action=abort` → turn 终止不续跑。retry 自动机制：`_sample_once` 命中可恢复错误先走 `RetryConfig`（默认 `max_attempts=3`）自动退避重试；**3 次耗尽**或确定性"等外部介入"类（`provider_auth` / `provider_quota` / `provider_balance`）才转 `SYSTEM_RETRY` 挂起。`ContentFilter` / `ContextOverflow` / `InvalidRequest` 这类确定性失败**不挂起**，照旧硬失败（resume 也救不回）。

#### Scenario: permission allow resume 执行 tool
- **WHEN** `Resume(resolutions={req: {"granted": true}})`，该 pending 是 permission
- **THEN** engine 执行 `related_call_id` 对应 tool（preapprove 后），回填其 `function_call_output`，turn 续采样

#### Scenario: system_retry 自动重试耗尽才挂起
- **WHEN** `_sample_once` 连续命中 retryable LLMError 且 retry 预算耗尽
- **THEN** 转 `SuspendSignal(reason=SYSTEM_RETRY, related_call_id=None)` → end_reason=suspended

### Requirement: 多挂起点并存 + batch resume

同一 turn 一批 tool call 可同时命中多个挂起点（如 permission + form 同批）。`dispatch_batch` **不 fail-fast**，整批收集所有 `SuspendSignal`，聚合为**一条** `SuspensionRecord`（多个 pending 共享 `record_id`）。resume 用 `{request_id: payload}` 一次补齐全部。

### Requirement: 子 thread resume 续跑链（call_skill 嵌套挂起）

`call_skill` 派发的子 skill 在**独立子 thread** 内运行（`TurnRunner.run_sub_skill` → `_spawn_sub_runner`，`history_buffer` 隔离）。子 turn 命中挂起点时，挂起 `SuspensionRecord` 落在**子 thread**，子 turn emit `turn_suspended`（`thread_id` = 子 thread）。

此时父的 `call_skill` SHALL **随之挂起**而非把子结果误当成功/失败回填：`_spawn_sub_runner` 在子 `outcome.end_reason == "suspended"` 时抛 `SuspendSignal(reason=CHILD_SKILL, related_call_id=父 call_id, detail={sub_thread_id, skill_id})`。该信号经父 `_dispatch_one` 捕获为 `outcome.suspend` → 父 `_BatchSuspend` → 父落自己的 `SuspensionRecord`（含一条 `CHILD_SKILL` pending），逐层上抛至根 → 根 emit `turn_suspended`。**子 thread 与根 thread 各 emit 一次 `turn_suspended`**（子携子 thread_id、根携根 thread_id）。

`Resume(thread_id=<子 thread_id>, resolutions=...)` SHALL 由 `AgentEngine._handle_resume` 在 `op.thread_id != self._thread_id` 时分流到 `_handle_child_resume` 续跑链：

1. **串链**：自根 `self._thread_id` 沿各层活跃挂起的 `CHILD_SKILL` pending（`detail.sub_thread_id`）向下串出 `[根, …, leaf]`，每层记 `(thread_id, entry_skill_id, 父 call_id)`。根 thread / entry skill 由 engine 自持，子层谱系由父挂起 record 的 pending detail 携带——**不依赖 `MessageStore.get_metadata`**（该协议无元数据查询）。串不到目标 leaf → emit `suspension_resolve_rejected(no_active_suspension)`。
2. **leaf 续跑**：用用户 `resolutions` 核销 leaf 的真实挂起（permission/form/data，复用 `SuspensionResolver` + gap 补齐），重建 `TurnRunner` 续跑子 turn → 拿 `final_text`（= 正常子 turn 完成回传）。
3. **逐层回传**：自 leaf 向上把每个父 `call_skill` 的 `function_call_output` 回填为子结果（= 正常 `run_sub_skill` 的 `ToolResult.ok`），落 resolved-marker 核销父 record，续跑父 turn；根用既有 `self._history` / `_build_and_run_runner` 收尾 → 根 emit `turn_completed(is_root=True)`。

任一层续跑若**又挂起**，该层各自 emit `turn_suspended`，续跑链在该层中止（上层 `call_skill` 仍挂起，等下一次 `Resume`）。续跑链各层 turn 的事件均带 `Resume` submission 的 `submission_id`；子层续跑 turn 标记 `is_root=False`（engine 注入非空 `call_stack`），仅根续跑 turn `is_root=True`。

> **R1**：`CHILD_SKILL` 是纯内核派发态（`sub_thread_id` / `skill_id` / `call_id` 均为基础调度 id），无业务概念。**R5**：续跑链全程基于 store 持久态（子 thread `load_thread` + 父 record pending detail），跨进程 resume 可行。

#### Scenario: 子 skill 内挂起 → Resume 子 thread → 续跑回传父 → 根完成
- **WHEN** 父 `call_skill` 派子，子内 `permission` 挂起 → `Resume(thread_id=<子 thread>, resolutions={req: {granted: true}})`
- **THEN** `turn_suspended` 携子 thread_id（≠ 根）；resume 后 `suspension_resolved`（leaf + 父各一）、子续跑输出落子 thread、被挂起 call 补回 `function_call_output`、整个 submission 以根 `turn_completed(is_root=True)` 收尾

### Requirement: 禁部分 resume

`SuspensionResolver.validate` SHALL 要求 `set(resolutions.keys()) == record.request_ids()`。缺某 pending 的 id、或带不存在的 id → `ResolveError` → emit `suspension_resolve_rejected`（禁 silent fallback；分批由业务侧攒齐再提交，避免半挂起态）。

#### Scenario: 部分 / 多余 resolution 被拒
- **WHEN** resolutions 缺一个 pending 的 request_id，或含 record 没有的 id
- **THEN** `ResolveError(incomplete_or_extra_resolutions: missing=... extra=...)` → `suspension_resolve_rejected`

### Requirement: 幂等（resolved-marker + 重复 resume 拒绝）

挂起断点的"活跃 / 已消费"判定**不靠改写**已落盘 item（JSONL 追加写不可变），而靠**追加一条 resolved-marker**：`system_injection` 且 `payload.source == "suspend_resolved"`、`text == "suspend_resolved:<record_id>"`。

`_find_active_suspension` 扫 history：返回**最后一条** `kind=="suspension"` 且其 `record_id` **未**出现在任何 resolved-marker 中的 record；否则返回 `None`。`_handle_resume` 成功后落 resolved-marker（标记本 record 已消费）。

> **resolved-marker 是纯内部记账，不进 LLM 视图**：`history_to_api_messages` 渲染时**跳过** `source == "suspend_resolved"` 的 `system_injection`（其它 `system_injection` 如 `business` / `memory_pre_evict` 保留）。否则它会渲染成对话中段 `role="system"` 消息——`openai_compat` provider 原样透传，严格 OpenAI-compat 代理拒绝中段 system → 400（`anthropic` / `gemini` provider 各自特判丢弃 / 转 user，`openai_compat` 不处理）。

#### Scenario: 重复 Resume（同 record 两次）被拒
- **WHEN** 第一次 Resume 成功（已落 resolved-marker），第二次再 Resume 同 thread
- **THEN** `_find_active_suspension` 返回 `None` → emit `suspension_resolve_rejected(reason="no_active_suspension")`，turn 不重复续跑

### Requirement: R4 取消丢弃挂起

挂起期间收到 `CancelTurn`（目标为挂起 turn 的 submission），`_cancel_active_suspension` SHALL 追加一条 resolved-marker 丢弃该挂起（与 resume 同机制），使其不再被 `_find_active_suspension` 返回；后续 Resume 命中 `no_active_suspension` 被拒。无匹配挂起则 no-op（保持 CancelTurn 宽容语义）。协程已退栈，不阻塞主 actor。

#### Scenario: 挂起中 CancelTurn → 丢弃
- **WHEN** turn 已 end_reason=suspended，对其 submission 发 `CancelTurn`
- **THEN** 落 resolved-marker（emit EngineLog）；之后 `Resume` 被拒为 `no_active_suspension`

### Requirement: tier-1 / tier-2 释放 + 跨进程重建（R5）

- **tier-1（默认，快）**：释放 turn 协程，engine 留在 Pool；同进程 resume 直接读内存 history。
- **tier-2（缩容 / 进程可退）**：Pool 驱逐 engine、进程退出。挂起真相全在 store（`suspension` item + `function_call`-无-`function_call_output` 的 history-gap）；之后凭 `thread_id` 从 JSONL 重建 + `Resume` 续跑。

**挂起真相** = 持久化的 `suspension` item + function_call-without-output gap。跨进程 resume 经 tier-2 重建测试证明（MockClient 模拟跨实例，从 JSONL + `SuspensionRecord` 重建续跑）。

#### Scenario: 跨进程重建续跑
- **WHEN** engine A 挂起后进程退出；engine B 从同 `thread_id` 重建（`resume_thread_id`），随后 `Resume`
- **THEN** `_find_active_suspension` 从重建 history 还原 record，配对续跑成功

## R1–R5 影响（见设计 §7）

| 红线 | 影响与落实 |
| --- | --- |
| **R1 业务零侵入** | `suspend/` 全 typed、无业务词；`detail` / `resolutions` payload 不透明 JSON，taifeng 不解析其 keys。`created_at` / `record_id` / `request_id` 由注入工厂提供（src 内不取系统时钟 / 随机）。 |
| **R2 Cache 友好** | resume 补齐 `function_call_output` 是 **tail append**（不动 head），符合 mid-turn 只改 tail；`turn_suspended.cache_invalidated` 标注 tier-2 跨进程必失效、tier-1 同进程尽量保 anchor。 |
| **R3 可观测** | 新增 `turn_suspended` / `suspension_resolved` / `suspension_resolve_rejected` EventMsg；挂起结局以独立终结态 `turn_suspended` 上事件流（携带 `thread_id` / `record_id` / `pending` / `cache_invalidated`），与 `TurnCompleted` 区分。 |
| **R4 可取消** | 挂起态可被 `CancelTurn` 丢弃（`_cancel_active_suspension`）；协程已退栈，不阻塞主 actor。 |
| **R5 可 resume** | `SuspensionRecord` 走既有 JSONL 追加写；复用 `resume_thread_id` 跨进程重建。resolved-marker 同样追加写、不改写历史。 |
