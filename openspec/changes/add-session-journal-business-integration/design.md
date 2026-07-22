## Context

`taifeng.conversation.journal` 已有隔离的 Phase 1 durable core：RFC 8785 canonical JSON、SHA-256
hash chain、BEGIN/envelope/COMMIT 原子 batch、同进程 live lease、durable ack 与 strict verification。
当前 Engine、LLM、Tool、Skill 和 legacy MessageStore 尚未使用该 core；业务主链的真实状态仍分散在
内存 history、JSONL transcript 与允许丢失的 EventMsg 中。

ADR 0025 规定 SessionJournal 是单 Session 执行历史的唯一可靠事实源，MessageStore 和 Timeline 只能是
可重建视图。当前变更只实现新 Session 的严格纵向切片：`UserMessage → LLM → 基础 Tool/同步
call_skill → assistant`。旧 Session、恢复、HITL、compaction、spawn 等能力不会在这一切片中被部分接入；
audit-required 模式必须在 effect 前拒绝它们。

工程约束包括：Python 3.12+、Pydantic frozen DTO、anyio 非阻塞 IO/取消屏蔽、依赖注入、中文注释与完整
docstring；conversation/loop/llm 基础层变更必须通过全量测试、Sim selfcheck、真实 LLM capability matrix
并刷新 ledger。详细字段与执行序列沿用已通过独立复审的
`docs/superpowers/specs/2026-07-22-journal-business-integration-design.md`。

## Goals / Non-Goals

**Goals:**

- 新建 audit-required Session 时先 durable 初始化 Journal，再启动 Engine。
- 为 submission、turn、LLM attempt/response、Tool、Skill、thread/session 和对话项提供版本化记录契约。
- 对 effect 执行 `durable intent → at-most-one live dispatch → durable outcome or UNKNOWN`。
- 确保任何已发布 LLM delta 都能由此前 durable 的 response checkpoint 重建。
- 只从 durable-acked `conversation_item` 更新 hot history 和可重建 MessageStore 物化层。
- Journal 故障后冻结当前 Session 的 effect gate；投影失败只标记 stale；其他 Session 不受影响。
- 明确 admission、CancelTurn、Shutdown、release/close 的并发顺序与幂等终结。
- 保持未启用 audit 的现有 API、执行语义、测试和 transcript 行为兼容。

**Non-Goals:**

- 不打开、迁移或恢复已有 Journal；不实现 `open_existing`、recovery lease、repair、reconcile 或 unfreeze。
- 不支持 HITL/审批、suspend/resume、compaction/rewind、memory、instruction 更新、hooks、声明式
  orchestration、detached spawn、barrier 或 peer。
- 不把 Journal 包从 `taifeng.conversation` 顶层导出为稳定公共 API。
- 不实现 payload redaction、加密、WORM、外置 blob 或内容引用型附件；本切片保存完整内联正文。
- 不承诺跨进程 exactly-once，也不提供真正实时的 token 流；audit 模式在 checkpoint 后批量发布 delta。
- 不 archive、merge 或迁移 legacy transcript。

## Decisions

### 1. Journal 是唯一可靠事实源，领域结果与对话项原子提交

所有进入对话历史的事实都表示为 versioned `conversation_item`，并与对应领域 outcome 在一个 Journal
batch 中提交：

```text
submission_accepted + conversation_item(user_message) + submission_applied
llm_response_committed + conversation_item(reasoning/assistant/function_call...)
tool_outcome_committed + conversation_item(function_call_output)
skill_dispatch_finished + thread_terminal + conversation_item(skill_outcome)
```

只有 durable ack 后才能更新 hot history 或物化投影。Tool outcome 只写 function call output，并引用 LLM
batch 已写入的唯一 function call item，避免重复历史项。

未选择 shadow dual-write，因为 Journal 失败后继续写 legacy store 会让审计事实与真实 effect 分叉。也未
立即删除 MessageStore，因为现有非 audit 路径仍依赖它；strict 模式只允许 Journal 驱动的可重建物化层。

### 2. 保持 core envelope，领域 payload 独立版本化

新增 `conversation/journal/records.py`。每种领域 payload 使用 `pydantic.BaseModel`，配置
`frozen=True, extra="forbid"`，并含 `payload_version=1`。现有 `JournalRecord` 顶层字段不变；
`turn_index`、`call_id` 等属于 payload。

Phase 1 初始化的 `session_started/thread_created/thread_bound` 是已有 V0 canonical vectors，不修改物理
格式或 record id。新 child thread 使用 V1 payload；decoder 同时识别初始化 V0 和新增 V1。

稳定 identity 为：

| 对象 | Identity |
|---|---|
| submission | submission id |
| root/child turn | `{thread_id}:{submission_id}:turn:{turn_index}` |
| LLM logical call | `{turn_id}:llm:{iteration}` |
| LLM attempt | `{llm_operation_id}:attempt:{retry_ordinal}` |
| tool call | `{turn_id}:tool:{call_id}` |
| skill dispatch | `{tool_operation_id}:skill:{target_skill_id}` |

除初始化 V0 外，record id 使用
`{operation_id}:{record_type}:{attempt_id-or-none}:{ordinal}`。相同 id/相同完整内容返回原 ack；相同 id/
不同内容由 core 拒绝。

### 3. 一个 Session 只有一个 SessionAuditCoordinator

新增 `loop/audit.py`。coordinator 持有 core、lease、expected seq、Session root cancellation、每个 active
turn 的 target cancellation、health、projection watermark 和 lifecycle/admission lock。root 与所有 child
thread 共用同一 coordinator/lease，通过 lineage 字段区分；`call_skill` 不创建第二个 writer。

coordinator 串行追加，只以 durable ack 推进 seq。第一次 Journal IO、integrity 或 ack-uncertain 失败会：

1. 保存第一个稳定失败；
2. 将 health 设为 `RECOVERY_REQUIRED`；
3. 关闭新 effect gate；
4. 取消 Session root 及 child subtree；
5. 后续 effect 返回同一 `SessionAuditFrozenError`。

投影失败调用 `mark_projection_stale()`，不改变 Journal health，也不取消 effect。

### 4. core 增加 lease-safe `close_session()`，EnginePool 只拥有 Session 生命周期

`JsonlSessionJournalCore.close_session(lease)` 在 registry/per-session lock 下验证完整 lease，等待在途 append，
标记 writer closed 并只移除该 Session writer。它不写任何领域事实。全局 core 仍归调用方所有，EnginePool
不得调用 `core.close()`。

EnginePool 是 Session 生命周期唯一 owner。`Shutdown`、`release()`、`close()` 只能请求同一个幂等
`coordinator.finish()`；其他组件不能直接写 terminal record 或关闭 lease。

### 5. lifecycle 与 submission admission 共用状态机

每个 audited Session 在同一 lock 下执行 `OPEN → FINISHING → CLOSED`：

- `AgentEngine.submit()` 在 OPEN 状态先 durable acceptance，再把携带 ack/record ids 的 token 入 actor queue；
  禁止 enqueue-first，因此队列中不存在未 accepted submission。
- 进入 FINISHING 的胜者原子关闭 intake，快照全部 durable-accepted queued/in-flight submissions，并创建
  唯一 finish future。
- FINISHING 后的新请求既不 durable accept 也不入队，返回 `SessionFinishingError`。
- accepted-but-queued submission 必须在 `session_ended` 前收敛。

`CancelTurn` 只触发目标 turn 及 child effect subtree 的 target token，不触发 Session root。freeze 和
Shutdown 才取消 Session root。首个 Shutdown 可在同一 lock 内赢得 FINISHING，登记唯一 shutdown id，
写 acceptance 后进入 finish；并发其他 Shutdown 在 acceptance 前拒绝。

生命周期 operation id 固定为 `{session_id}:lifecycle:end`；thread terminal 按 thread id 排序并使用稳定
ordinal，session ended 使用 ordinal 0。finish 成功只 close 一次；terminal commit 失败时执行 emergency
close 并报告 `audit_complete=false`，不能把结果缓存成成功。

### 6. MessageStore 只作为 durable-ack 驱动的可重建投影

新增 `conversation/journal/projector.py`。它只接受 coordinator 刚提交且 record id 被 ack 覆盖的
`conversation_item` envelopes，将 payload 显式反序列化为现有 ResponseItem，按 Journal seq 更新默认
JSONL 物化层，并维护 per-thread projected seq。

bootstrap 使用预分配 thread id，写入 `audit_required=true`、`journal_session_id` 和 schema version。
投影文件可以删除并从 Journal 重放；resume/verify/recovery 永远不能把它当作权威输入。带 audit marker
的 transcript 在 legacy resume 中稳定拒绝，防止降级绕过。

### 7. EnginePool 先初始化 Journal，再建立 transcript/Engine

audit 配置通过依赖注入提供 core、writer id、附件上限等，不从环境变量读取。EnginePool 构造期先验证
静态 capability matrix，然后：

1. 预分配 root thread id；
2. `create_session()` 原子写 V0 初始化三记录；
3. 创建 coordinator；
4. projector 用同 id 建立带 audit marker 的空 transcript；
5. 成功后才构造、warmup 并启动 AgentEngine。

projector bootstrap 失败时由 EnginePool 调用唯一 finish 路径写 terminal/session end 后释放 lease；若终结
也失败则 emergency close 并暴露 `audit_complete=false`。

### 8. 每个真实 LLM attempt 都经过 observer；checkpoint 先于 delta

新增 `llm/audit.py` 的 `ModelAttemptObserver` 与可观测 ModelClient 适配器。每次网络 dispatch 前，
`before_attempt()` durable 写 `llm_request_committed` 并返回 permit。attempt 完成/失败/取消后，
`after_attempt()` 在 cancellation-independent shield 内 durable 写 attempt-specific
`llm_response_checkpoint`；ack 后才能向 TurnRunner 发布缓冲的原序 delta，或开始下一次内部 retry。

observer/ack 异常使该 attempt 为 UNKNOWN 并冻结，不得 retry。当前 provider 每个 `stream()` 只有一个网络
attempt；未来若增加内部 retry，必须显式接入 observer 才能通过 audit capability gate。

最终 successful logical call 由 TurnRunner 原子提交 `llm_response_committed` 和 conversation items；ack 后
才允许 Tool effect 或 turn terminal。

### 9. Tool batch 先提交全部 intent，再进行取消独立的终态收敛

ToolSpec 增加稳定 `effect_kind`、`reconciliation` 与 `can_suspend` metadata；legacy 构造保持兼容，但 audit
模式拒绝缺失 metadata 或可 suspend 的 Tool。hooks、permission 和 HITL 在 strict path 中不存在，所以
effective arguments 等于已解析 arguments。

Tool batch 先按 call index 原子提交所有 executable/rejected intents，再启动 effect task。每个 branch 捕获
异常为结构化状态；parent cancel 时 best-effort 取消 siblings，并在独立有界 shield 中为每个已提交 intent
产生一个 terminal outcome：

- 尚未越过 dispatch gate，或 runtime 明确证明 effect 未发生/已知终止，才是 `cancelled`；
- 仅捕获取消异常、超时或无法撤销的外部动作是 `unknown`；
- 明确结果是 success/error/rejected。

全部 outcomes 与对应 function call outputs 按 call index 原子提交。任何 UNKNOWN 冻结 Session，不进入下一
次 LLM。

### 10. 同步 `call_skill` 共享 Session Journal 并记录完整 lineage

outer Tool intent durable 后提交含完整 skill definition/body/hash/arguments/provenance 的 `skill_selected`。
quota rejection 写 `skill_dispatch_finished(rejected, started_record_id=None)`，不伪造 child。

quota 通过后预分配 child thread id，原子提交
`skill_dispatch_started + thread_created + thread_bound + conversation_item(child seed)`。ack 后应用 hot child
history；child transcript 投影失败只标 stale。child turn 使用 child thread id 与 parent submission id 生成
独立 turn identity。

child success/error/cancel 后原子提交
`skill_dispatch_finished + thread_terminal + conversation_item(skill_outcome)`，outer Tool 收敛器随后提交
自己的 outcome/output。任何 suspension 在产生 HITL effect 前拒绝；声明为 non-suspending 的实现若仍抛
SuspendSignal，写 error outcome 并冻结，以暴露 capability 声明违约。

### 11. capability gate 明确拒绝所有未接入路径

audit-required 只允许：新 Session、UserMessage/CancelTurn/Shutdown、默认 JSONL projector/directory、无
hooks/permission/compressor/memory/instruction、atomic/composite + 同步 call_skill、无 spawn/peer、可观察
attempt 的 model client、metadata 完整且不 suspend 的 Tool。

EnginePool 构造期验证静态配置，submission gateway 验证 Op，TurnRunner 在每个 effect 前再次检查动态路径。
拒绝发生在 effect 前，并写稳定 submission rejection 或 turn failure（Session 已 FINISHING/CLOSED 时除外，
直接返回生命周期错误）。

### 12. 稳定错误和附件不依赖任意 Python 表示

`StableErrorV1` 保存 code、稳定类型名、failure class、可选安全消息/descriptor hash、retryable；任意异常不
调用 `repr()`，不持久化地址、traceback 或 secret。调用方输入 canonical/附件校验失败写
`submission_rejected` 且不冻结；runtime-owned DTO canonical 失败是内核不变量破坏，effect 前冻结。

附件只接受完整内联 base64 content，并在 acceptance 前验证 media type、size、SHA-256、单项/总大小上限。
临时路径、缺失正文、digest 不符和引用型附件稳定拒绝。

## Risks / Trade-offs

- [Audit 模式不再实时逐 token 展示] → checkpoint ack 前缓冲 delta；用明确的批量可见性换取“用户所见必可审计”。
- [完整正文和内联附件增加 Journal 体积] → 注入严格大小上限；blob 外置、加密与保留策略另立变更。
- [跨多个基础模块，回归面较大] → 以 records/core/projector/coordinator/Engine/LLM/Tool/Skill 小切片 TDD，
  每片独立提交并保留 legacy mode 回归。
- [投影失败导致 UI/磁盘视图暂时落后] → Journal 与 hot history 保持可执行，记录 projected seq/stale，允许按
  seq 幂等重放；不把投影错误误判为 effect 审计失败。
- [effect 已发生但 outcome 无法 durable] → 标记 UNKNOWN、冻结并禁止通用自动重试；本切片不假装已恢复。
- [同进程 lease 无法阻止进程重启后的错误接管] → audit marker 阻止 legacy resume，并明确只保证当前 live
  pool；跨进程 recovery/open 后续实现。
- [严格能力门禁暂时减少可用功能] → audit 为显式 opt-in；非 audit path 保持现状，后续按能力逐项扩展契约。
- [生命周期并发复杂] → admission/finish 共用一个状态锁、一个 finish future、稳定 record ids 和单次 close，
  通过 release-vs-Shutdown、多 Shutdown、accepted-but-queued 测试证明。

## Migration Plan

1. 先交付 `close_session()`、领域 DTO/record factory、projector 与 coordinator，不接业务 effect。
2. 加入 audit config/capability gate 和 Journal-first bootstrap；默认 `audit=None`，生产行为不变。
3. 接入 acceptance-before-enqueue、target cancel 与 lifecycle finish。
4. 依次接入 LLM checkpoint、Tool convergence、同步 `call_skill` lineage；每步通过 focused 与 legacy tests。
5. 完成完整 Sim 端到端矩阵、full pytest/mypy/Ruff、自检和 OpenSpec strict validation。
6. 获得外部 provider 授权后运行真实 LLM capability matrix，刷新 ledger；没有该证据不得标完成、archive
   或 merge。

回滚时关闭 audit opt-in 并回退本变更提交；legacy mode 的存储和 API 未迁移。已经创建的 audited Journal
保留为事实，不删除、不转换成 legacy transcript，也不允许 legacy resume。

## Open Questions

无阻断问题。跨进程 recovery、HITL/审批、compaction/rewind、spawn/peer、payload 加密/外置与 Timeline
均已明确拆分为后续独立变更。
