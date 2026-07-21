# ADR 0025: SessionJournal 作为会话执行唯一事实源

- 状态：Accepted
- 日期：2026-07-21
- 关联：ADR 0005（Submission / EventMsg 双总线）、ADR 0008（Store 协议解耦）
- 参考：OpenAI Codex Rollout、OpenAI Agents Trace、Anthropic Managed Agents Session Event Stream

## 背景

Taifeng 当前有两条不同语义的数据线：

1. `MessageStore` 可靠追加 `ResponseItem`，支撑 resume、rewind 和 compaction；
2. `EventMsg` 以 `put_nowait` 投递实时语义事件，队列满允许丢弃，消费异常不得阻塞主 actor。

这种分层适合交互和可观测，但不能形成完整审计事实：用户最初输入虽然进入
`MessageStore`，Timeline 若只读 `EventMsg` 可能看不到；HITL、审批、上下文、Skill 快照、
LLM request、工具副作用及异常窗口也分散在不同路径。把可靠写入直接塞进 `_emit` 又会改变
EventMsg 的低延迟、可丢、不阻塞语义，并使所有业务路径依赖一个重型事件总线。

OpenAI Codex 使用 canonical rollout 保存 Session/ResponseItem/Context 等权威记录，再生成查询投影
和实时通知；Anthropic Managed Agents 也把 persisted event 作为 authoritative record，把 delta
明确定位为 best-effort preview。Taifeng 采用同一范式，并补上审计场景所需的 fail-closed、
完整性校验和外部副作用恢复语义。

## 决策

引入通用内核协议 `SessionJournal`，作为单个 Session 执行历史的**唯一可靠事实源**。

- `MessageStore` 成为 Journal 的对话兼容视图，不再与 Journal 双写；
- Audit Timeline 从 Journal 投影，不从 EventMsg 反推；
- EventMsg 保持轻量实时投影，允许丢失，丢失后可从 Timeline 补读；
- token、reasoning、终端输出等 delta 仅作瞬时预览；完成后的权威内容必须写 Journal；
- Journal 关键写失败时冻结当前 Session，禁止继续模型、Skill、工具、spawn、resume 等执行；
- 不冻结其他 Session，不把持久化 IO 串到全局 actor。

`SessionJournal` 是通用执行内核概念，不包含 tenant、业务审批流或宿主领域字段。

## 权威数据模型

### JournalEnvelope

每一行是自描述、可校验的 envelope：

```python
@dataclass(frozen=True)
class JournalEnvelope:
    schema_version: int
    session_id: str
    seq: int
    writer_epoch: int
    record_id: str
    operation_id: str | None
    attempt_id: str | None
    recorded_at: datetime
    occurred_at: datetime | None
    record_type: str
    submission_id: str | None
    thread_id: str | None
    turn_id: str | None
    parent_record_id: str | None
    causation_id: str | None
    correlation_id: str | None
    actor: ActorRef
    previous_hash: str
    payload_hash: str
    record_hash: str
    payload: Mapping[str, JsonValue]
```

约束：

- `seq` 在单 Session 内从 1 严格递增；
- `writer_epoch` 是 Session lease 的 fencing token；旧 writer 的 epoch 不能继续追加；
- `record_id` 是 Session 内幂等键；重复追加必须比较调用方提供的完整 canonical `JournalRecord`
  （含 actor、session/thread/turn、operation/attempt、causation/correlation、occurred_at、record type
  和 payload），只排除 writer 分配的 seq、epoch、recorded_at、previous/hash；任一差异必须抛
  `JournalConflictError`；
- `record_hash` 覆盖规范化 envelope 和 `previous_hash`，形成 hash chain；
- `recorded_at` 由 Journal writer 在提交时生成，业务方时间可另存 payload，不能替代提交时间；
- `occurred_at` 是事件发生时间，缺失时显式为 `None`，Timeline 以 `recorded_at` 排序；
- `turn_id` 跨 suspend/resume 保持稳定，`attempt_id` 区分 retry、rewind 和恢复尝试；
- `actor` 至少包含 `kind` 与 `source`；宿主未提供 principal 时写 `kind="unknown"`，不得省略；
- schema 只允许向后兼容增加字段；破坏性变更提升 `schema_version` 并提供迁移器。

规范序列化采用 RFC 8785 JSON Canonicalization Scheme；hash 使用 SHA-256。`payload_hash` 覆盖规范化
payload；`record_hash` 覆盖除 `record_hash` 自身之外的完整 envelope。首条记录的 `previous_hash`
使用 64 个字符的零值。Hash chain 用于检测缺失和修改，不声称能抵抗拥有整个文件写权限的攻击者；
不可抵赖场景需由后端增加签名 checkpoint、WORM 或外部锚点。

所有调用方值在进入 Journal 前转换为 canonical DTO：datetime 统一 RFC 3339 UTC、Enum 使用稳定值、
Path 使用绝对 file URI、bytes 使用 base64 + SHA-256 描述，mapping key 必须是字符串，浮点仅允许有限
IEEE-754 值，NaN/Infinity 拒绝。`ActorRef` 是版本化 DTO，不接受任意对象。不同语言实现必须通过
同一组 canonical JSON/hash conformance vectors。

### 必须覆盖的 Record 类型

| 领域 | 权威 Record | 必含内容 |
| --- | --- | --- |
| Session | `session_started` / `session_ended` / `session_frozen` | engine 配置、模型/provider、cwd、代码版本、终态和原因 |
| Thread | `thread_created` / `thread_bound` / `thread_terminal` | thread→session 绑定、parent、预留 child id、终态 |
| 上下文 | `context_snapshot` / `context_compacted` | 实际送模上下文、system/developer/instruction、压缩前后锚与 cache 信息 |
| 用户入口 | `submission_accepted` / `submission_rejected` / `submission_applied` | 每一种 `Op` 的原始 payload、附件身份与 hash、应用结果 |
| Instructions | `instruction_resolved` / `instruction_failed` | scope、source、完整内容、版本/hash、解析结果 |
| Skill | `skill_selected` / `skill_dispatch_started` / `skill_dispatch_finished` | skill id、版本/hash、实际 SKILL.md 快照、参数、父子谱系、结果/错误 |
| LLM | `llm_request_committed` / `llm_response_checkpoint` / `llm_response_committed` | 实发 request、provider request id、有序 normalized items、usage、finish/error |
| Tool | `tool_intent_committed` / `tool_outcome_committed` | 名称、原始参数、hook/policy 后有效参数、完整结果/data、错误、耗时 |
| 审批 | `approval_requested` / `approval_decided` | call id、策略、展示内容、选项、actor、决定、理由、有效范围 |
| HITL | `hitl_requested` / `hitl_submitted` / `hitl_resolved` | 问题/schema、原始人类输入、自动决议标记、关联 call/request id |
| 挂起恢复 | `suspension_created` / `resolution_submitted` / `suspension_settled` | record/pending 快照、全部 resolution、partial/full/abort 结果 |
| 编排 | `spawn_intent` / `spawn_outcome` / `barrier_state` | parent/预留 child thread、skill、handle、barrier revision、成员快照、终态 |
| 对话 | `conversation_item` | 原始 `ResponseItem`，供 MessageStore 兼容视图读取 |

“完整”指 Taifeng 实际拥有并用于执行或展示的内容完整保存。模型未返回的隐藏推理不属于可审计输入，
不得伪造；provider 返回的 encrypted reasoning item 按原值保存。二进制附件可以使用内容寻址引用，
但 Journal 必须保存 digest、大小、媒体类型和可验证位置，不能只留易失临时路径。

## 协议

```python
class SessionJournal(Protocol):
    async def create_session(
        self,
        session: SessionDescriptor,
        *,
        expected_seq: int = 0,
        cancel: CancellationToken | None = None,
    ) -> SessionLease: ...

    async def open_existing(
        self,
        session_id: str,
        *,
        expected_seq: int,
        cancel: CancellationToken | None = None,
    ) -> SessionOpenResult: ...

    async def acquire_recovery_lease(
        self,
        session_id: str,
        *,
        observed_tail: JournalVerification,
        cancel: CancellationToken | None = None,
    ) -> RecoveryLease: ...

    async def append(
        self,
        record: JournalRecord,
        *,
        lease: SessionLease,
        expected_seq: int,
        durability: Durability = Durability.COMMITTED,
        cancel: CancellationToken | None = None,
    ) -> JournalAck: ...

    async def append_batch(
        self,
        records: Sequence[JournalRecord],
        *,
        lease: SessionLease,
        expected_seq: int,
        durability: Durability = Durability.COMMITTED,
        cancel: CancellationToken | None = None,
    ) -> JournalAck: ...

    async def load(
        self, session_id: str, *, after_seq: int = 0
    ) -> AsyncIterator[JournalEnvelope]: ...

    async def verify(self, session_id: str) -> JournalVerification: ...

    async def health(self, lease: SessionLease) -> JournalHealthReport: ...
    async def reconcile(
        self,
        lease: RecoveryLease,
        operation_id: str,
        resolution: ReconciliationResult,
        *,
        expected_seq: int,
        durability: Durability = Durability.COMMITTED,
        cancel: CancellationToken | None = None,
    ) -> JournalAck: ...
    async def repair_tail(
        self,
        lease: RecoveryLease,
        repair: TailRepairDecision,
        *,
        expected_seq: int,
        durability: Durability = Durability.COMMITTED,
        cancel: CancellationToken | None = None,
    ) -> JournalAck: ...
    async def unfreeze(
        self,
        lease: RecoveryLease,
        *,
        expected_seq: int,
        durability: Durability = Durability.COMMITTED,
        cancel: CancellationToken | None = None,
    ) -> SessionRecoveryResult: ...
    async def close_session(self, lease: SessionLease) -> None: ...
```

`SessionDescriptor` 必须包含创建 Session 所需的稳定配置和
`root_thread: RootThreadDescriptor`；后者至少含预分配的 `thread_id`、entry skill、source、tags
与 canonical extra。`create_session` 在一个原子批次中提交
`session_started + thread_created + thread_bound`，成功后才返回执行 lease，因此调用方不需要在
“尚无 lease”和“必须先初始化”之间循环依赖。`SessionDescriptor` 还必须携带稳定且唯一的
`creation_operation_id` 和 `writer_id`。只有同一进程内仍持有 live lease 的同一 creation operation
重试，才允许从 writer cache 返回同一个 lease；其他 writer、其他 operation 或进程重启后的
`create_session` 即使 descriptor 相同也返回 busy/already-exists，绝不复制或复活旧 lease。崩溃后的
接管统一走 `open_existing` 获取更高 fencing epoch；descriptor 任一差异始终冲突。

`close_session` **只释放 lease 和 writer 资源，不写业务事实**。正常终结必须由调用方先用
`append(session_ended, durability=COMMITTED)` 获得 durable ack，再调用 `close_session`；紧急关闭
允许仅释放资源，但 health report 必须标记 `audit_complete=False`。是否存在 `session_ended` 是
Timeline 的终态事实，不由资源清理动作暗中推断。

每个 record type 在接入执行路径前必须有独立的版本化 payload DTO 契约，定义必填/可选字段、状态转移、
关联键和 canonical vectors；仅在本 ADR 的表格中登记名称不构成可实施契约。Phase 1 只提供通用
envelope、`JsonValue` 校验与存储机制，不宣称已实现 LLM/Tool/HITL 等领域 record serializer。

`append_batch` 对一个逻辑边界原子提交，例如“审批决定 + tool intent”或“tool outcome +
conversation_item”，并为整个批次返回一个 `JournalAck`。`JournalAck` 必含 committed seq 范围、
record ids、尾 hash、writer epoch 和实际
durability。取消只允许发生在提交开始前；一旦写入开始，短暂 shield 直至明确成功或失败，避免
调用方取消后不知道记录是否落盘。shield 和 fsync 都有有界 deadline；超时视为 ack 不确定，
Session 进入 `RECOVERY_REQUIRED`，不能假定未写。

`open_existing` 只在 verify 完整且无未决 effect 时发放普通新 epoch lease；发现 torn tail、ack 不确定、
未闭合 intent 或 frozen marker 时只返回 `RECOVERY_REQUIRED`，不发执行 lease。恢复方先 verify，再用
`acquire_recovery_lease` 获取更高 fencing epoch；旧 lease 立即失效。`repair_tail`、`reconcile`、
`unfreeze` 都是 COMMITTED Journal 追加，必须携带 recovery lease 和 expected seq。只有全部 UNKNOWN
完成显式裁决、tail 校验通过并 durable 写入 `session_recovered` 后，`unfreeze` 才返回
`SessionRecoveryResult {ack, execution_lease}`；其中 ack 证明恢复记录已提交，execution lease 使用更高
fencing epoch。调用方拿到完整结果前 effect gate 保持关闭，RecoveryLease 随成功返回原子失效。

append/append_batch 在持有单 Session writer 锁后按固定顺序处理幂等与 CAS：

1. 先按 `record_id` 查询已提交记录；单条记录完全相同则返回原始 ack，即使调用方携带的是 ack 丢失前的
   旧 `expected_seq`；内容不同则 `JournalConflictError`；
2. 批次重试只有在全部 record id 均存在、内容完全相同、属于同一个已提交批次且顺序连续时返回原始
   batch ack；部分重叠、跨批次重组或内容差异一律冲突；
3. 没有命中已提交幂等记录时才校验 `expected_seq == committed_tail_seq`，不等则 CAS 冲突；
4. 未提交的 torn batch 中出现相同 record id 不算幂等成功，Session 保持 `RECOVERY_REQUIRED`。

## 默认 JSONL 实现

默认 `JsonlSessionJournal` 每 Session 单文件、单异步 writer worker：

1. 规范 JSON 序列化；
2. 通过文件锁/后端 lease 获取单调 `writer_epoch`，跨进程拒绝旧 writer；
3. 用 `expected_seq` 做 compare-and-append，在 writer 内分配连续 seq 和 hash chain；
4. 单记录直接写一个 envelope；批次写成 `BEGIN + envelopes + COMMIT` 校验 frame；
5. 恢复时只有带有效 COMMIT、记录数和 batch hash 均匹配的批次可见，部分批次全部隐藏；
6. 一次批次追加后 `flush + fsync`，新建/rename 时同步目录，随后才返回 durable ack；
7. 文件 IO/fsync 在 anyio worker thread 执行，不阻塞 event loop；
8. 仅在 durable ack 后允许关键动作继续；
9. SQLite 只能作为可重建投影，永远不能领先 JSONL。

批次 frame 使用保留字段 `"__journal_frame__"`，避免与 envelope 混淆：

- `BEGIN` 包含 `frame_version`、`batch_id`、`record_count`、调用方 `expected_seq` 和按输入
  `JournalRecord` 计算的 `batch_payload_hash`；
- 中间 envelope 才分配 `seq` 并进入正常 record hash chain；`BEGIN/COMMIT` 不占 `seq`、不成为
  `previous_hash` 节点；
- `COMMIT` 包含相同 batch identity、`first_seq/last_seq`、ordered record hash 汇总和 `tail_hash`；
- reader 只有在 BEGIN、数量、输入 hash、连续 seq/hash chain 与 COMMIT 全部匹配时才一次性暴露整个
  批次；frame 本身不是 Timeline record；
- torn/无效批次不改变可见 `committed_tail_seq`，因此后续合法 `expected_seq` 仍指向 BEGIN 前的尾部，
  但 writer 必须先进入 `RECOVERY_REQUIRED`，禁止普通 append 覆盖或越过物理残尾；
- `repair_tail` 只允许在 recovery lease 下把物理尾裁到最后一个 committed frame，并追加 durable
  recovery record。具体 frame canonical schema 与字节级 hash vectors 由 Phase 1 capability contract 固化。

启动或 resume 必须逐行验证 JSON、seq、payload hash 和 hash chain。允许识别 torn final line，
但不得静默跳过：Session 进入 `RECOVERY_REQUIRED`，由显式恢复操作裁决。中间损坏恒为不可自动恢复。

旧 `ResponseItem` JSONL 作为 `legacy_unverified` 历史只读加载。严格模式 resume 前必须导入新 Journal，
保存原始文件 digest 和逐行来源；未导入不得执行新的外部副作用。

## 执行边界与失败语义

### 入口规则

Session 创建先原子提交 `session_started + thread_created + thread_bound`，再向调用方返回可提交的
Engine。Submission 先写 `submission_accepted`，再改变内存 history 或开始 turn。写失败时拒绝该
Submission，原始输入仍由调用方保有，Session 进入冻结态。

Submission gateway 必须先把 Op 转成对应的版本化 canonical DTO，再写 `submission_accepted`。它覆盖
当前 `Op` union 的全部成员，而不是只覆盖对话：`UserMessage`、
`InjectSystemMessage`、`InjectUserInput`、`Cancel`、`CompactNow`、`ThreadRollback`、`UpdateBudget`、
`RefreshSnapshot`、`UpdateInstructions`、`Resume`、`Rewind`、`SendToPeer`、`Shutdown`。新增 Op 必须
先登记 journal DTO serializer；否则 strict engine 拒绝启动。

`UpdateInstructions.new_source` 等任意 `InstructionSource` 只保存稳定的 source kind、配置 DTO、版本、
代码/artifact hash；实际解析内容由 `instruction_resolved` 保存。`Resume.resolutions`、
`Rewind.new_args` 等自由结构必须先通过 JSON-compatible DTO 校验。闭包、provider 实例或其他无法
规范化的对象在 `submission_accepted` 前拒绝，写 `submission_rejected`，其中只含 Op kind、稳定类型名、
安全 descriptor/hash 和拒绝原因，绝不调用可能泄密或不稳定的任意 `repr()`。被拒对象从未进入执行，
因此不构成执行事实缺失。

### 外部调用规则

所有可能产生费用、网络调用或外部状态变化的 effect 必须遵循：

```text
durable intent → at-most-one live dispatch → durable outcome or UNKNOWN
```

- LLM：先提交完整 request，再调用 provider，先提交 response checkpoint 才投递对应 UI delta，
  最后提交有序 normalized item 列表、partial/aborted/complete 终态、usage 和 error；
- Tool/Skill：先提交完整参数和 effect metadata，再执行，再提交结果；
- Spawn：先提交 child intent，再创建 child，结果关联 parent intent；
- Approval/HITL：请求和决定都是独立事实，决定必须在执行被批准动作前提交。

所有 effect intent（LLM、Tool、Skill、Spawn、Barrier/peer wake）必须包含：

- `effect_kind`: `pure | idempotent | reconcilable | external_non_idempotent`；
- `idempotency_key`（适用时）；
- `reconciliation`：恢复时查询、重试或人工确认策略。

### 写入异常

| 失败位置 | 行为 |
| --- | --- |
| intent 提交前/时失败或 ack 不确定 | 不启动动作；冻结当前 Session |
| 动作失败且 outcome 成功提交 | 正常记录失败，可按 failure policy 继续 |
| 动作已完成、outcome 提交失败 | 冻结 Session；状态 `UNKNOWN/RECOVERY_REQUIRED`，禁止通用自动重试 |
| Timeline/EventMsg 投影失败 | 不影响 Journal；记录投影落后水位，可重放补齐 |
| SQLite 投影失败 | 不影响 Journal；标记 stale，后台重建 |

Journal 不承诺跨崩溃 exactly-once。未匹配 intent 在恢复时一律是 `UNKNOWN`；仅当 effect 明确幂等，
或 reconciler 证明未执行/已成功后，才允许显式重试或补写 outcome。

异常后若连 `session_frozen` 都无法写入，冻结状态至少保存在 engine 内存和 logger；重启时由“未配对 intent”
扫描重建 `RECOVERY_REQUIRED`，因此不能依赖最后一条错误记录本身。

## Session 级冻结

Engine 维护 `JournalHealth = healthy | frozen | recovery_required`：

- 第一次观察到 Journal 失败后，原子 gate 禁止启动新的 LLM、Tool、Skill、spawn、barrier 和 peer effect；
- `frozen/recovery_required` 拒绝 UserMessage、Resume、spawn、LLM、Skill 和 Tool；
- Cancel、读取 Timeline、verify、reconcile、关闭 Session 仍允许；
- child/spawn 共用所属 Session Journal；任一关键写失败冻结同 Session 的 root 和全部 child；
- 其他 AgentEngine/Session 使用独立 writer 和健康状态，不受影响。

已经越过 dispatch gate 的并发 effect 只能 best-effort cancel，不能承诺撤销；它们全部进入 `UNKNOWN`
并等待 reconcile。Journal 不可用时执行的紧急 Cancel/close 是安全降级动作，必须在内存 health report
中标记 `audit_complete=False`，恢复后补记，不能声称当时已可靠审计。

## MessageStore 兼容

默认存储由 session-bound `JournalConversationView` 提供 MessageStore 兼容能力，但物理上只写 Journal：

- view 在构造时绑定 `SessionLease` 和 thread→session 路由；
- `MessageStore.append(item)` 在同一 Session Journal 转成 `conversation_item`；
- `load_thread` 过滤 Journal 中的 `conversation_item` 并保序返回；
- rewind、resume、compaction 无需理解其他 Record；
- 新 Timeline 读取所有 Record；
- 禁止默认实现同时写旧 transcript 和新 journal。

根 thread 必须在 EnginePool 返回 engine 前完成 Session/Thread 原子绑定。strict 模式下第三方后端
只有一个 canonical Journal 写入口；所谓 MessageStore 实现只能是调用该入口的 session-bound facade，
或从 Journal 可重建的只读/物化投影。投影禁止独立 append，resume/verify/recovery 永远以 Journal
records 为准；即便数据库能在同一事务中双写表，也不能把物化表声明为第二事实源。
仅实现旧 `MessageStore` 的后端进入显式 `compat` 模式并暴露 `audit_complete=False`，不能伪装成完整审计。

## Timeline 投影

`JournalTimelineProjector` 按 `seq` 生成稳定 TimelineItem：

- 展示 `recorded_at`，前端不得用浏览器 `now()` 代替；
- 支持按 turn、submission、actor、record_type、call_id、skill_id 筛选；
- user 输入、HITL 回答和审批决定默认显示完整内容；敏感展示由调用方访问控制决定；
- 每项可回链原始 `record_id/seq`；
- 领域 record 是唯一权威表示，projector 直接映射 EventMsg；不另写重复的 `semantic_event`；
- 投影至少一次，EventMsg 携带可选 `journal_record_id/journal_seq` 供去重；
- 实时 EventMsg 仅作为“新水位到了”的通知，客户端断线后用 `after_seq` 补读；
- LLM delta 只有在对应 `llm_response_checkpoint` durable 后才对 UI 可见，因此用户已看到的内容必可审计。

## 敏感数据与保留

原始 Journal 始终保存可恢复的完整 payload，或保存同等可靠、内容寻址且不可变的加密 blob 引用。
redaction 只发生在 Timeline、导出和权限视图，不能改变唯一事实源。投影视图提供：

- `full`：有权调用方读取完整内容；
- `redacted`：确定性脱敏，同时返回 redaction manifest 和原 payload hash；
- `metadata_only`：仅查询元数据并显式返回 `audit_complete=False`。

访问控制、加密密钥和保留期由宿主后端负责；OTel 永不承载正文 Journal payload。

## 被否方案

### 让 EventMsg 直接成为 WAL

否决。EventMsg 高频、可丢、吞异常，承担 UI 实时投影；同步 fsync 会改变 R4 语义并阻塞 actor。

### 独立 MessageStore + 独立 AuditJournal 双写

否决。两条可靠线无法共享所有后端的事务，崩溃窗口会产生两个互相矛盾的事实源。

### 在所有函数上加审计装饰器

否决。装饰器看不到真实提交点、HITL 关联和外部副作用结果，且把审计散布进业务实现。

### 只保存 Trace/OTel

否决。Trace 适合调试和性能分析，不保证内容完整、可 resume 或 fail-closed。

## 后果

正面：

- 用户、上下文、Skill、模型、Tool、HITL、审批形成统一可回放时间线；
- EventMsg 继续轻量，不污染现有业务订阅；
- MessageStore 不再与审计双写；
- 崩溃窗口和未知副作用变成显式状态；
- JSONL 是真相源，SQLite/UI 随时可重建。

代价：

- 关键边界增加一次可靠 append；
- 完整 payload 增加磁盘和隐私治理成本；
- 第三方 MessageStore 需升级为 SessionJournal 才能声明 strict 完整审计；
- 旧 transcript 只能标为未验证或显式迁移。

## 分阶段实施边界

本 ADR 是总决策，不作为单个大爆炸实施计划。每一阶段均需自己的 capability contract、测试和 commit：

1. **Phase 1 — Journal durable core**：版本化核心 DTO/错误/ack/lease、三个初始化 record DTO、canonical
   JSON 与 hash vectors、单 Session JSONL create/append/append_batch/load/verify、单 writer fencing、
   torn tail 与 partial batch 检测。它只提供新建 Session 的孤立 backend primitive；不实现
   `open_existing`、不发恢复 execution lease，也不接 Engine、MessageStore、Timeline 或真实 effect。
   完整 `SessionJournal` 协议在 Phase 2 补齐恢复/open 语义前不作为稳定公共 API 导出；
2. **Phase 2 — recovery 与 freeze gate**：recovery lease、repair/reconcile/unfreeze 状态机、Session 级
   effect gate 和 UNKNOWN 收敛；
3. **Phase 3 — Session/Thread/Submission 接入**：把 Phase 1 已有初始化批次接入 EnginePool，补全部
   Op DTO 和 `JournalConversationView`，保持 resume/rewind/compaction 兼容；
4. **Phase 4 — effect 边界接入**：LLM/Tool/Skill/Approval/HITL/Spawn/Barrier 的 intent、checkpoint、
   outcome 与失败窗口；
5. **Phase 5 — Timeline 与迁移**：Timeline/redaction、legacy import、投影重建和最终全量真实 LLM 回归。

Phase 1 不提前实现 projection、legacy migration、blob 外置、签名/WORM 或初始化三类之外的领域
serializer。**任何阶段只要变更 `src/taifeng/{llm,loop,context,conversation}/`，都必须严格遵守仓库红线：**
先跑零消耗 selfcheck，再全量运行真实 LLM capability matrix 并提交更新后的
`docs/real-llm-ledger.{json,md}`；不得由本 ADR 豁免。

## 验收标准

1. 初始用户输入、后续 UserMessage、HITL 原始回答均能从 Timeline 完整读取；
2. system/developer/instruction、实际 Skill 快照和实发 LLM request 可重放；
3. 每个审批请求都有唯一决定或未决终态；
4. 每个 Tool/Skill/LLM intent 都有 outcome，或启动恢复时标为 UNKNOWN；
5. EventMsg 队列丢弃不造成 Timeline 缺口；
6. JSONL 尾部撕裂、hash 不匹配、seq 跳号均被检测，不静默跳过；
7. Journal 写失败被观察后当前 Session 不再启动新的模型、工具、Skill、spawn、barrier 或 peer 副作用；
8. 一个 Session 冻结不影响其他 Session；
9. MessageStore resume/rewind/compaction 回归不变；
10. 基础层变更通过全量 pytest，并按仓库红线刷新真实 LLM capability ledger。
11. 并发 effect 在冻结前已启动时全部进入 UNKNOWN，恢复不会自动重复非幂等操作；
12. `append_batch` 中途 kill -9 后没有部分批次对 reader 可见；
13. 两个 writer 竞争同一 Session 时旧 fencing epoch 的追加被拒绝；
14. 任一已投递给 UI 的 LLM 文本都能从 durable response checkpoint 重建。
15. 相同 record id 但 actor/thread/causation 不同的追加必须冲突；
16. torn tail 或 ack 不确定后，旧 lease 失效，只有更高 epoch recovery lease 能 repair/reconcile；
17. 每种 Op DTO 有跨实现 canonical hash vector；NaN、闭包和未登记对象在执行前被拒并安全留痕；
18. 删除全部 MessageStore 物化数据后可从 Journal 重建，且 resume 结果不变。
