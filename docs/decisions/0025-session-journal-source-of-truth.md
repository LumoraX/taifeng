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
    record_id: str
    recorded_at: datetime
    record_type: str
    submission_id: str | None
    thread_id: str | None
    turn_id: str | None
    parent_record_id: str | None
    previous_hash: str | None
    payload_hash: str
    record_hash: str
    payload: Mapping[str, JsonValue]
```

约束：

- `seq` 在单 Session 内从 1 严格递增；
- `record_id` 是幂等键，重复追加返回原 ack，不产生第二条事实；
- `record_hash` 覆盖规范化 envelope 和 `previous_hash`，形成 hash chain；
- `recorded_at` 由 Journal writer 在提交时生成，业务方时间可另存 payload，不能替代提交时间；
- schema 只允许向后兼容增加字段；破坏性变更提升 `schema_version` 并提供迁移器。

### 必须覆盖的 Record 类型

| 领域 | 权威 Record | 必含内容 |
| --- | --- | --- |
| Session | `session_started` / `session_ended` / `session_frozen` | engine 配置、模型/provider、cwd、版本、终态和原因 |
| 上下文 | `context_snapshot` / `context_compacted` | 实际送模上下文、system/developer/instruction、压缩前后锚与 cache 信息 |
| 用户入口 | `submission_accepted` / `submission_rejected` | UserMessage、Resume、Cancel、Shutdown 的原始 payload、附件身份与 hash |
| Instructions | `instruction_resolved` / `instruction_failed` | scope、source、完整内容、版本/hash、解析结果 |
| Skill | `skill_selected` / `skill_dispatch_started` / `skill_dispatch_finished` | skill id、版本/hash、实际 SKILL.md 快照、参数、父子谱系、结果/错误 |
| LLM | `llm_request_committed` / `llm_response_committed` | 实发 ApiRequest、provider request id、完整最终 ResponseItem、usage、错误 |
| Tool | `tool_intent_committed` / `tool_outcome_committed` | 名称、完整参数、effect kind、幂等键、结果、错误、耗时 |
| 审批 | `approval_requested` / `approval_decided` | call id、策略、展示内容、选项、actor、决定、理由、有效范围 |
| HITL | `hitl_requested` / `hitl_submitted` / `hitl_resolved` | 问题/schema、原始人类输入、自动决议标记、关联 call/request id |
| 挂起恢复 | `suspension_created` / `resolution_submitted` / `suspension_settled` | record/pending 快照、全部 resolution、partial/full/abort 结果 |
| 编排 | `spawn_intent` / `spawn_outcome` / `barrier_state` | parent/child thread、skill、handle、等待集合、终态 |
| 对话 | `conversation_item` | 原始 `ResponseItem`，供 MessageStore 兼容视图读取 |
| Timeline | `semantic_event` | 低频权威语义事件；实时 EventMsg 由它投影 |

“完整”指 Taifeng 实际拥有并用于执行或展示的内容完整保存。模型未返回的隐藏推理不属于可审计输入，
不得伪造；provider 返回的 encrypted reasoning item 按原值保存。二进制附件可以使用内容寻址引用，
但 Journal 必须保存 digest、大小、媒体类型和可验证位置，不能只留易失临时路径。

## 协议

```python
class SessionJournal(Protocol):
    async def append(
        self,
        record: JournalRecord,
        *,
        durability: Durability = Durability.COMMITTED,
        cancel: CancellationToken | None = None,
    ) -> JournalAck: ...

    async def append_batch(
        self,
        records: Sequence[JournalRecord],
        *,
        durability: Durability = Durability.COMMITTED,
        cancel: CancellationToken | None = None,
    ) -> Sequence[JournalAck]: ...

    async def load(
        self, session_id: str, *, after_seq: int = 0
    ) -> AsyncIterator[JournalEnvelope]: ...

    async def verify(self, session_id: str) -> JournalVerification: ...
```

`append_batch` 对一个逻辑边界原子提交，例如“审批决定 + tool intent”或“tool outcome +
conversation_item”。取消只允许发生在提交开始前；一旦写入开始，短暂 shield 直至明确成功或失败，
避免调用方取消后不知道记录是否落盘。

## 默认 JSONL 实现

默认 `JsonlSessionJournal` 每 Session 单文件、单 writer：

1. 规范 JSON 序列化；
2. 在 writer 锁内分配 `seq` 和 hash chain；
3. 追加完整一行；
4. `flush` 后执行 `fsync`；新建/rename 时同步目录；
5. 仅在 durable ack 后允许关键动作继续；
6. SQLite 只能作为可重建投影，永远不能领先 JSONL。

启动或 resume 必须逐行验证 JSON、seq、payload hash 和 hash chain。允许识别 torn final line，
但不得静默跳过：Session 进入 `RECOVERY_REQUIRED`，由显式恢复操作裁决。中间损坏恒为不可自动恢复。

旧 `ResponseItem` JSONL 作为 `legacy_unverified` 历史只读加载。严格模式 resume 前必须导入新 Journal，
保存原始文件 digest 和逐行来源；未导入不得执行新的外部副作用。

## 执行边界与失败语义

### 入口规则

Submission 先写 `submission_accepted`，再改变内存 history 或开始 turn。写失败时拒绝该 Submission，
原始用户输入仍由调用方保有，Session 进入冻结态。

### 外部调用规则

所有可能产生费用、网络调用或外部状态变化的动作必须遵循：

```text
durable intent → execute once → durable outcome
```

- LLM：先提交完整 request，再调用 provider，再提交最终 response/error；
- Tool/Skill：先提交完整参数和 effect metadata，再执行，再提交结果；
- Spawn：先提交 child intent，再创建 child，结果关联 parent intent；
- Approval/HITL：请求和决定都是独立事实，决定必须在执行被批准动作前提交。

`tool_intent_committed` 必须包含：

- `effect_kind`: `pure | idempotent | external_non_idempotent`；
- `idempotency_key`（适用时）；
- `reconciliation`：恢复时查询、重试或人工确认策略。

### 写入异常

| 失败位置 | 行为 |
| --- | --- |
| intent 提交前/时失败 | 不执行动作；冻结当前 Session |
| 动作失败且 outcome 成功提交 | 正常记录失败，可按 failure policy 继续 |
| 动作已完成、outcome 提交失败 | 冻结 Session；状态 `UNKNOWN/RECOVERY_REQUIRED`，禁止通用自动重试 |
| Timeline/EventMsg 投影失败 | 不影响 Journal；记录投影落后水位，可重放补齐 |
| SQLite 投影失败 | 不影响 Journal；标记 stale，后台重建 |

异常后若连 `session_frozen` 都无法写入，冻结状态至少保存在 engine 内存和 logger；重启时由“未配对 intent”
扫描重建 `RECOVERY_REQUIRED`，因此不能依赖最后一条错误记录本身。

## Session 级冻结

Engine 维护 `JournalHealth = healthy | frozen | recovery_required`：

- `frozen/recovery_required` 拒绝 UserMessage、Resume、spawn、LLM、Skill 和 Tool；
- Cancel、读取 Timeline、verify、reconcile、关闭 Session 仍允许；
- child/spawn 共用所属 Session Journal；任一关键写失败冻结同 Session 的 root 和全部 child；
- 其他 AgentEngine/Session 使用独立 writer 和健康状态，不受影响。

## MessageStore 兼容

默认 `JsonlMessageStore` 同时实现 `MessageStore` 和 `SessionJournal`，但物理上只写 Journal：

- `MessageStore.append(item)` 转成 `conversation_item`；
- `load_thread` 过滤 Journal 中的 `conversation_item` 并保序返回；
- rewind、resume、compaction 无需理解其他 Record；
- 新 Timeline 读取所有 Record；
- 禁止默认实现同时写旧 transcript 和新 journal。

第三方 store 若要启用 strict 模式，必须实现 `SessionJournal`，或使用同一事务后端同时实现两个协议。
仅实现旧 `MessageStore` 的后端进入显式 `compat` 模式并暴露 `audit_complete=False`，不能伪装成完整审计。

## Timeline 投影

`JournalTimelineProjector` 按 `seq` 生成稳定 TimelineItem：

- 展示 `recorded_at`，前端不得用浏览器 `now()` 代替；
- 支持按 turn、submission、actor、record_type、call_id、skill_id 筛选；
- user 输入、HITL 回答和审批决定默认显示完整内容；敏感展示由调用方访问控制决定；
- 每项可回链原始 `record_id/seq`；
- 实时 EventMsg 仅作为“新水位到了”的通知，客户端断线后用 `after_seq` 补读。

## 敏感数据与保留

完整记录会包含 prompt、上下文、工具参数和输出。内核提供 `JournalContentPolicy` 注入点，但不绑定业务：

- `full`：本地默认，保存 Taifeng 实际拥有的全部内容；
- `redacted`：调用方提供确定性 redactor，同时保存 redaction manifest 和原 payload hash；
- `metadata_only`：不满足完整审计，只能显式选择且 `audit_complete=False`。

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

## 验收标准

1. 初始用户输入、后续 UserMessage、HITL 原始回答均能从 Timeline 完整读取；
2. system/developer/instruction、实际 Skill 快照和实发 LLM request 可重放；
3. 每个审批请求都有唯一决定或未决终态；
4. 每个 Tool/Skill/LLM intent 都有 outcome，或启动恢复时标为 UNKNOWN；
5. EventMsg 队列丢弃不造成 Timeline 缺口；
6. JSONL 尾部撕裂、hash 不匹配、seq 跳号均被检测，不静默跳过；
7. Journal 写失败后当前 Session 不再产生模型、工具、Skill 或 spawn 副作用；
8. 一个 Session 冻结不影响其他 Session；
9. MessageStore resume/rewind/compaction 回归不变；
10. 基础层变更通过全量 pytest，并按仓库红线刷新真实 LLM capability ledger。
