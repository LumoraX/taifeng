# SessionJournal 普通业务主链接入设计

## 背景

`taifeng.conversation.journal` 已交付隔离的 Phase 1 durable core，具备 RFC 8785
canonical JSON、SHA-256 hash chain、原子 batch、同进程 live lease、durable ack 与
strict verification。独立正式验收确认该 core 的 68 项测试通过，但业务执行路径没有引用
Journal。

本变更交付第一个严格纵向切片：新 Session 的普通
`UserMessage → LLM → 基础 Tool/call_skill → assistant` 主链。它遵守 ADR 0025：

- Journal 是执行事实与对话项的唯一可靠事实源；
- MessageStore 只消费已提交的 `conversation_item`，不能领先 Journal；
- effect 遵循 `durable intent → at-most-one live dispatch → durable outcome or UNKNOWN`；
- audit-required Session 不支持的能力在 effect 前稳定拒绝，不能绕过 Journal。

## 目标

- 为新 Session 建立单一 coordinator、连续 seq 和不可降级的 audit-required 标记。
- 完整记录 submission、turn、LLM attempt/checkpoint/commit、Tool、Skill 和
  `conversation_item`。
- 先 durable checkpoint 再发布对应 UI delta。
- Journal 故障只冻结当前 Session；对话投影失败只标记 stale，可从 Journal 重建。
- 保持未启用 Journal 的所有现有行为兼容。

## 非目标

- 不打开或迁移已有 Journal，不实现 `open_existing`、recovery lease 或 unfreeze。
- 不支持 HITL、审批、suspend/resume、compaction、rewind、detached spawn、peer、memory、
  instruction 更新、hooks 或声明式 orchestration。
- 不把 SessionJournal 从 `taifeng.conversation` 顶层导出为稳定公共 API。
- 不实现 payload redaction、blob 外置、加密或 WORM；audit-required 模式保存完整正文。
- 不提供真正实时的 LLM token 流；audit-required 模式在 durable checkpoint 后批量发布 delta。

## 方案与事实源边界

采用 Journal-first、fail-closed。未采用 shadow dual-write，也不立即删除 legacy store。

每个需要进入对话历史的事实，与其领域 outcome 在同一个 Journal batch 中提交。例如：

```text
llm_response_committed + conversation_item(reasoning?) + conversation_item(assistant)
tool_outcome_committed + conversation_item(function_call) + conversation_item(function_call_output)
submission_accepted + conversation_item(user_message) + submission_applied
```

只有 batch durable ack 后，hot history 才应用其中的 `conversation_item`；MessageStore projector
也只消费这些已提交 item。投影失败不改变 Journal 和 hot history，只设置
`projection_stale=true`，后续可按 seq 重放。MessageStore 不再产生独立事实。

## Phase 1 core 的必要增量

现有 core 缺少单 Session 资源释放。本变更只增加以下可在 live-process 内实现的能力：

```python
async def close_session(self, lease: SessionLease) -> None:
    """在单 Session writer lock 内关闭并移除 live writer；不写领域事实。"""
```

`close_session` 验证完整 lease；等待该 Session 在途 append，标记 closed，再从 registry 移除。
其他 Session writer 不受影响。现有 `close()` 仍用于外部 owner 最终关闭全部 writer。

Journal core 由调用方创建并注入 EnginePool，所有权仍属于调用方；EnginePool 只调用
`close_session`，不调用全局 `close()`。正常终结必须先 durable 写 `session_ended`，再释放 lease。
紧急关闭若无法写终结记录，只释放 lease并记录 `audit_complete=false` 到 logger/introspection。

本变更不增加 `open_existing`，因此关闭后的同一 Journal 不能重新执行。

## 组件边界

### 领域 payload DTO 与 record factory

新增 `conversation/journal/records.py`。所有 payload 使用 `pydantic.BaseModel`，配置
`frozen=True, extra="forbid"`，并在进入 core 前递归转换为 JsonValue。不得传任意 Python 对象。

`JournalRecord` 现有顶层字段保持不变：

- `session_id`、`record_id`、`record_type`、`actor`；
- `operation_id`、`attempt_id`、`occurred_at`；
- `submission_id`、`thread_id`、`turn_id`；
- `parent_record_id`、`causation_id`、`correlation_id`；
- `payload`。

`JournalRecord.schema_version` 由 record factory 固定填写，writer 原样复制到 envelope；seq、hash、
`recorded_at` 和 writer epoch 才由 writer 分配。`turn_index`、`call_id`、`parent_call_id` 属于对应
payload DTO，不新增错误的 JournalRecord 顶层字段。每个 payload 含 `payload_version=1`。

### SessionAuditCoordinator

新增 `loop/audit.py`。每个活跃 Session 恰有一个 coordinator，持有 core、lease、
expected seq、root cancellation、health 和 projected seq。它提供：

- `record()` / `record_batch()`：只以 durable ack 推进 seq；
- `commit_conversation_batch()`：提交领域 record 与有序 conversation items；
- `ensure_effect_allowed()`：任何 effect 前检查 health；
- `freeze()`：第一次失败原子转 `FROZEN/RECOVERY_REQUIRED` 并取消 root token；
- `finish()`：写 thread/session terminal records，再 `close_session()`；
- `mark_projection_stale()`：不冻结执行事实，只报告投影水位落后。

child thread 共享 root coordinator 和 lease，通过 thread/turn/call lineage 区分。不得为
`call_skill` 创建第二个 Session writer。

### JournalConversationProjector

新增 `conversation/journal/projector.py`。输入只能是 coordinator 刚获得 durable ack 的
`conversation_item` record，不接受任意 ResponseItem 双写。它：

- 将 record payload 还原为现有 `ResponseItem`；
- 按 Journal seq 更新只读/可重建的 MessageStore 物化层，不调用独立事实 append 入口；
- 维护 per-thread `projected_seq`；
- 写失败时标记 stale 并返回，不抛成 Journal failure；
- bootstrap 时用预分配 thread id 建立 default transcript 与 metadata。

本切片只支持默认 JSONL MessageStore/ThreadDirectory 的可重建物化投影栈。投影文件可删除并从
Journal 重放；resume/verify/recovery 不得读取它作为权威输入。自定义 store/directory、IndexHook
在 audit-required 模式启动时拒绝。

### Engine/LLM/Tool 接入

- EnginePool 预分配 root thread id，先创建 Journal，再让 projector 创建同 id 的 transcript。
- MessageStore/Jsonl 默认实现增加“使用调用方给定 thread id 创建空投影”的内部能力；非 audit
  调用仍由 store 自己生成 id。
- AgentEngine 先提交 submission batch，再从其中 conversation item 更新 hot history。
- TurnRunner 只从 coordinator ack 后的 item 更新 history/投影。
- provider 每个真实网络 attempt 通过 `ModelAttemptObserver` 在发送前获取 durable permit；没有
  observer 能力的 ModelClient 在 audit-required 模式启动时拒绝。
- tool dispatcher 在 audit-required 模式使用无 hooks/permission/suspension 的严格路径。

## Operation identity 与 record id

稳定 identity：

| 对象 | Identity |
|---|---|
| submission | 现有 `submission_id` |
| turn | `{submission_id}:turn:{turn_index}` |
| LLM logical call | `{turn_id}:llm:{iteration}` |
| LLM network attempt | `{llm_operation_id}:attempt:{retry_ordinal}` |
| tool call | `{turn_id}:tool:{call_id}` |
| skill dispatch | `{tool_operation_id}:skill:{target_skill_id}` |
| child thread | 预分配 `thread_id` |

record id 由 factory 统一生成：

```text
{operation_id}:{record_type}:{attempt_id-or-none}:{ordinal}
```

ordinal 是同一 operation/type 内稳定的零基序号，conversation item 使用其在原子 batch 中的稳定
item index。相同逻辑重试产生相同完整 JournalRecord；payload 变化则由 core 报 conflict。

## 版本化 payload DTO

以下表中的字段均为必填，除非标为 optional。自由 mapping 必须先通过 JsonValue 校验。

### Submission 与 turn

| Record | Payload V1 |
|---|---|
| `submission_accepted` | `payload_version, op_kind="user_message", turn_index, text, attachments, source` |
| `submission_applied` | `payload_version, accepted_record_id, conversation_item_ids` |
| `submission_rejected` | `payload_version, op_kind, stable_error, input_descriptor_hash` |
| `turn_started` | `payload_version, turn_index, entry_skill_id, skill_snapshot_version, model, budget_snapshot` |
| `turn_completed` | `payload_version, turn_index, end_reason, iterations, usage, final_item_ids` |
| `turn_failed` | `payload_version, turn_index, stable_error, effect_state` |
| `turn_cancelled` | `payload_version, turn_index, cancellation_reason, effect_state` |

附件 DTO 为 `AttachmentRefV1 {kind, media_type, size, sha256, uri?}`；不保存不可验证临时对象。

### LLM

| Record | Payload V1 |
|---|---|
| `llm_request_committed` | `payload_version, turn_index, iteration, provider, model, api_request, effect_kind, idempotency_key?, reconciliation` |
| `llm_response_checkpoint` | `payload_version, request_record_id, retry_ordinal, status, normalized_items, usage?, provider_request_id?, stable_error?` |
| `llm_response_committed` | `payload_version, request_record_id, checkpoint_record_id, status, normalized_items, usage, provider_request_id?, stable_error?` |

LLM status 为 `complete | error | cancelled | unknown`。`normalized_items` 保持 provider 顺序，包含
assistant text、reasoning、tool calls 和 provider 可见 encrypted item；不伪造隐藏推理。

每次真实 provider attempt 都有独立 `attempt_id`。`ModelAttemptObserver.before_attempt()` 必须在网络
dispatch 前 durable 写 `llm_request_committed` 并返回 permit；`after_attempt()` 只回传 attempt 元数据，
最终 checkpoint/commit 由 TurnRunner 收敛。provider 内 retry ordinal 从 0 单调增加并接受测试注入。

### Tool

| Record | Payload V1 |
|---|---|
| `tool_intent_committed` | `payload_version, turn_index, iteration, call_id, name, arguments_raw, effective_arguments, parallel_safe, effect_kind, idempotency_key?, reconciliation` |
| `tool_outcome_committed` | `payload_version, intent_record_id, call_id, name, status, output, data, duration_ms, stable_error?` |

Tool status 为 `success | error | rejected | cancelled | unknown`。ToolSpec 增加稳定 audit metadata：
`effect_kind` 与 `reconciliation`；`idempotency_key` 默认使用 session+call id，仅表示本次 live dispatch
身份，不声称外部系统支持幂等。

本切片禁止 hooks 和 permission policy，因此 `effective_arguments == parsed arguments`。invalid JSON 在
ToolCallRequest 建立前形成 `turn_failed`，无 tool intent；not-offered call 形成 intent +
`tool_outcome_committed(status="rejected")`，不调用 runtime。

### Skill 与 thread

| Record | Payload V1 |
|---|---|
| `skill_selected` | `payload_version, call_id, skill_id, version, definition_hash, body_hash, full_definition, arguments, selection_origin, confidence?` |
| `skill_dispatch_started` | `payload_version, selected_record_id, call_id, parent_call_id?, child_thread_id, call_stack, arguments` |
| `skill_dispatch_finished` | `payload_version, started_record_id?, call_id, child_thread_id?, status, end_reason?, final_text?, usage?, stable_error?` |
| `thread_created` | `payload_version, entry_skill_id, source, parent_thread_id?` |
| `thread_bound` | `payload_version, session_id, thread_id, call_id?` |
| `thread_terminal` | `payload_version, status, end_reason, stable_error?` |
| `session_ended` | `payload_version, status, reason, audit_complete` |

Skill status 为 `success | error | rejected | cancelled | unknown`。quota rejection 允许
`skill_selected → skill_dispatch_finished(status="rejected", started_record_id=None)`；没有伪造 started。

### Conversation item

`conversation_item` payload 为：

```text
payload_version, item_version=1, item_kind, thread_id, item_id,
canonical ResponseItem fields, source_record_id
```

factory 为每一种当前 ResponseItem kind 提供显式 serializer；未知 kind 在 effect 前拒绝 audit-required
启动，不能把 `model_dump()` 当作无版本稳定契约。

### Stable error

`StableErrorV1`：`code, class_name, failure_class, safe_message?, descriptor_hash?, retryable`。

- `class_name` 只保存稳定类型名，不含模块地址。
- 只有 Taifeng 定义的公开错误或 ToolResult 输出可进入 `safe_message`。
- 任意异常不调用 `repr()`；使用稳定 code、类型名和 canonical descriptor hash。
- 用户输入/附件 canonical 失败发生在 submission acceptance 前：写安全
  `submission_rejected`，不冻结 Session。
- runtime-owned DTO 无法 canonicalize 是内核不变量破坏：effect 前冻结 Session。
- Journal IO/integrity/ack-uncertain 一律冻结或进入 recovery-required。

## 配置与 capability gate

audit-required 模式仅在以下矩阵成立时启动：

| 维度 | 允许 | 稳定拒绝 |
|---|---|---|
| Op | `UserMessage`, `CancelTurn`, `Shutdown` | 其他全部 Op |
| Session | 新建 | `resume_thread_id`、已有 Journal |
| Store | 默认 JSONL projector + 默认 directory | custom store/directory、IndexHook |
| Hooks/approval | 无 | 任意 hook、permission policy、HITL prompter |
| Context | 无 compressor、无 memory、无 instruction update | compaction/rewind/memory/instruction layers |
| Skill | atomic/composite + 同步 call_skill，无 orchestration | 声明式 orchestration、suspending skill |
| Spawn/peer | 禁用 | detached spawn、barrier、peer send/wake |
| LLM | 实现 ModelAttemptObserver 的 provider | 无 attempt observer 的 client |
| Tool | audit metadata 完整且不 suspend | request_user_input、未知/可 suspend tool |

EnginePool 构造期验证静态配置和 tool/provider capability；Submission gateway 验证 Op；TurnRunner 在
每个 effect gate 再验证动态路径。遇到 unsupported capability 时，在 effect 前提交稳定
`submission_rejected` 或 `turn_failed`，不执行该能力。

默认 transcript metadata 写 `audit_required=true`、`journal_session_id` 和 `journal_schema_version`。
任何 legacy resume 读取到该标记时，无论调用方是否注入 Journal，都稳定拒绝，防止降级绕过。该保证只
覆盖 Taifeng 默认投影和 API；不声称阻止调用方直接篡改文件或跨进程恢复。

## 精确执行顺序

### Bootstrap

1. 校验 audit capability matrix。
2. 预分配 root thread id，不写 MessageStore。
3. `create_session` 原子提交 `session_started + thread_created + thread_bound`。
4. projector 用该 id 创建带 audit marker 的空 transcript。
5. projector 失败：追加 `thread_terminal + session_ended(audit_complete=true, reason=projection_bootstrap_failed)`，
   close_session，Engine 创建失败。若终结记录也失败，freeze 后紧急 close，audit_complete=false 只进日志。
6. 成功后才启动并返回 Engine。

### UserMessage

1. canonicalize；非法输入 durable 写 `submission_rejected`，返回稳定拒绝，Session 保持 healthy。
2. 原子提交 `submission_accepted + conversation_item(user_message) + submission_applied`。
3. ack 后更新 hot history，再投影 MessageStore；投影失败只标 stale。
4. durable 写 `turn_started`，进入 LLM。

### LLM 与 UI delta

1. build/preflight 完成，生成 logical operation id。
2. provider 每个网络 attempt 在 dispatch 前通过 observer durable 写
   `llm_request_committed(attempt_id=...)`。
3. TurnRunner 缓冲 provider delta，不发 EventMsg。
4. stream 明确 complete/error/cancel 后，在 cancellation-independent shield 内 durable 写
   `llm_response_checkpoint`，包含当前完整 normalized items 和 status。
5. checkpoint ack 后按原顺序发布对应 EventMsg delta；因此任何已展示内容都能由 checkpoint 重建。
6. 原子提交 `llm_response_committed + conversation_item(reasoning/assistant/function_call...)`。
7. ack 后更新 hot history并投影；只有此后才能开始 tool effect 或 turn terminal。

provider retry 的每个 attempt 都先有 request record；失败 attempt 的 checkpoint status=error，下一 attempt
使用新 attempt_id。最终 logical call committed record causation 指向最后 checkpoint，并 correlation 到同一
logical operation。

### Tool batch 收敛

1. 解析 JSON、visibility 和 audit capability preflight；hooks/policy 在该模式不存在。
2. 将所有 executable/rejected request 的 `tool_intent_committed` 按 call index 原子提交。
3. 为 executable calls 启动受 coordinator gate 保护的 task；每个 task 捕获所有异常为结构化状态。
4. parent cancel 时 best-effort 取消 siblings；使用 cancellation-independent 有界 shield 收敛每个已提交
   intent。明确完成写 success/error/cancelled，无法确认写 unknown。
5. 按 call index 原子提交全部 `tool_outcome_committed`，并为每个 call 同批提交
   `conversation_item(function_call/function_call_output)`。
6. ack 后更新 history/投影。任何 unknown 使 coordinator 进入 recovery-required，不开始下一次 LLM。

`asyncio.gather` 必须改为不因单个未预期异常丢失其他 outcome 的收敛器；每个 intent 恰有一个 terminal
outcome 或 unknown。Journal finalization 使用独立 shield，不能复用已取消 scope。

### call_skill

1. Tool intent 已 durable 后，提交 `skill_selected`（含完整 skill 快照）。
2. spawn quota preflight。拒绝则提交 `skill_dispatch_finished(rejected)`，不创建 child。
3. quota 通过后预分配 child thread id，原子提交
   `skill_dispatch_started + thread_created + thread_bound + conversation_item(child seed)`。
4. ack 后 projector 创建 child transcript、hot child history 应用 seed，再运行 child TurnRunner。
5. child normal/error/cancel 后原子提交 `skill_dispatch_finished + thread_terminal`。
6. outer tool 收敛器随后提交 `tool_outcome_committed + parent conversation items`。
7. 任何 suspension 为 unsupported：在产生 HITL effect 前 gate 拒绝；若自定义 tool/skill 仍抛
   SuspendSignal，提交 error outcome 并冻结以暴露能力声明违约。

### Turn/Session terminal

- 正常 turn：最后一个原子 batch 为 `turn_completed` 与最终 conversation items（若有）。
- failure/cancel：在独立 shield 中写 `turn_failed` 或 `turn_cancelled`；若存在未闭合 effect，状态为
  unknown 并冻结。
- `EnginePool.release(session)`：先提交所有活跃 thread 的 `thread_terminal`，再 `session_ended`，最后
  `close_session(lease)`。
- `EnginePool.close()` 对每个 Session 串行执行 release；不调用外部 owner 的 core.close。

## 失败语义

| 失败点 | 结果 |
|---|---|
| canonical 用户输入失败 | submission_rejected；不冻结 |
| intent/ack 不确定 | 不启动 effect；freeze/recovery-required |
| effect 明确失败且 outcome durable | 记录 error；按现有 failure policy 终结，不自动视为 unknown |
| effect 后 outcome/checkpoint 写失败 | freeze；该 intent=unknown；禁止自动重试 |
| projection 任一点失败 | Journal/hot history 已提交；projection stale，可按 seq 重放，不冻结 |
| EventMsg 失败 | 不影响 Journal；UI 可从 checkpoint/Timeline 补读 |
| sibling tool 取消/超时 | 每个 intent 分别 cancelled 或 unknown；批量 durable 收敛 |

第一次 Journal failure 原子关闭 effect gate并取消 root/children；其他 Session 不受影响。若
`session_frozen` 无法写，内存 health 与 logger 仍 fail-closed。由于没有 open/recovery，本切片只保证当前
live pool 内不可继续；进程重启后 audited marker 阻止 legacy resume，但不宣称可以安全恢复执行。

## 测试策略

### Contract/unit

- 每个 payload DTO 的必填/extra/enum/canonical vectors。
- operation/attempt/record id 稳定性与冲突。
- ResponseItem serializer 覆盖所有允许 kind，未知 kind 拒绝。
- StableError 不泄露 repr、地址或 secret。
- close_session lease、Session 隔离和 global owner 边界。

### Integration/ordering

- checkpoint-before-delta：在 checkpoint 前模拟崩溃，断言 UI 无 delta；ack 后 delta 可从 Journal 重建。
- submission、LLM、tool 和 call_skill 的精确 success/error/cancel/freeze 序列表。
- provider 内部 retry：每次真实 attempt 有唯一 attempt id/request，最终 commit lineage 正确。
- 并行 tools：部分完成、unexpected exception、parent cancel、outcome unknown 和 sibling 收敛。
- 每个 MessageStore bootstrap/append 部分失败点：Journal 完整、hot history 正确、projection stale、可重放。
- quota rejection、child-create projection failure、child error/cancel、三层 lineage、thread terminal。
- intent 写失败时 effect spy=0；outcome 写失败后后续 LLM/Tool/Skill spy=0。
- 一个 Session frozen，另一个正常。

### Capability gates

- 每个 unsupported Op、resume、hooks、permission、HITL、compressor、memory、instruction、orchestration、
  spawn/peer、suspending tool/provider capability 都在 effect 前拒绝。
- audited marker 无 Journal 的 legacy resume 拒绝。
- 非 audit 模式现有行为/测试完全不变。

### Repository gates

- focused journal/loop/skill/tool/llm pytest、mypy、ruff。
- full mypy 与 full pytest。
- 更新 `docs/architecture/conversation.md`、`agent-loop.md`、`llm-client.md`、capability contract/index 和
  `docs/capability-matrix.md`。
- real-LLM selfcheck、完整 capability matrix 和两份 ledger。
- OpenSpec strict validate、git diff check、独立 code/spec review。

## 后续变更

1. HITL/审批、suspend/resume、compaction/rewind、spawn/peer/memory/instructions。
2. 持久化 fencing epoch、open_existing、recovery lease、repair/reconcile/unfreeze。
3. Timeline 投影、legacy import 和 MessageStore 全量 Journal 重建。
4. payload 保留、加密、redaction、blob 外置与 WORM。
