# SessionJournal Durable Core（Phase 1）能力契约

> 状态：Experimental。关联 ADR 0025。本契约只覆盖孤立 durable core，不表示 Engine 已获得完整审计能力。

## 1. 范围

本能力在 `taifeng.conversation.journal` 内提供 Session 级 canonical record、hash chain、原子 JSONL batch、
同进程 live writer fencing、strict load/verify。它不替换 `JsonlMessageWriter` / `JsonlMessageStore`，不接入
Engine、EventMsg、resume、Timeline 或外部 effect，也不从 `taifeng.conversation` 顶层导出。

Phase 1 不提供 `open_existing`、跨进程接管、recovery lease、repair/reconcile/unfreeze、legacy migration、
redaction、blob 外置或签名/WORM。调用方不能把本阶段描述为完整审计真相源集成。

## 2. 数据契约

所有 DTO 使用 frozen、`extra="forbid"` 的 Pydantic model。调用方 payload 只接受递归 `JsonValue`：
`None | bool | int | finite float | str | list[JsonValue] | dict[str, JsonValue]`。
DTO 会递归复制并冻结嵌套 dict/list；`create_session` 与 `append_batch` 还必须在首次 await 前重新验证并快照
完整调用方输入。append 快照同时预计算 caller fingerprints；后续 BEGIN、编码、幂等比较和 committed
索引必须复用同一组快照与 fingerprints，不得在 IO 返回后从原 DTO 重算。

### 2.1 Actor 与 Session 初始化

```python
class ActorRef:
    version: int = 1
    kind: str
    source: str
    principal_id: str | None = None

class RootThreadDescriptor:
    thread_id: str
    entry_skill_id: str
    source: str = "user"
    tags: tuple[str, ...] = ()
    extra: dict[str, JsonValue] = {}

class SessionDescriptor:
    schema_version: int = 1
    session_id: str
    creation_operation_id: str
    writer_id: str
    root_thread: RootThreadDescriptor
    config: dict[str, JsonValue]
```

`create_session` 必须在一个 batch 内依次提交：

1. `session_started`，record id = `<creation_operation_id>:session_started`；
2. `thread_created`，record id = `<creation_operation_id>:thread_created`，causation 指向 1；
3. `thread_bound`，record id = `<creation_operation_id>:thread_bound`，causation 指向 2。

### 2.2 JournalRecord

```python
class JournalRecord:
    schema_version: int = 1
    session_id: str
    record_id: str
    record_type: str
    actor: ActorRef
    payload: dict[str, JsonValue]
    operation_id: str | None = None
    attempt_id: str | None = None
    occurred_at: datetime | None = None
    submission_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    parent_record_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
```

record fingerprint 覆盖上述全部字段的 canonical 表示。它不得排除 actor、scope、causation、occurred_at
或 payload；相同 record id 但 fingerprint 不同必须冲突。

### 2.3 JournalEnvelope

```python
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
    payload: dict[str, JsonValue]
```

- `seq` 单 Session 从 1 严格递增；`committed_tail_seq` 初始为 0。
- 首条 `previous_hash` 必须为 `"0" * 64`；以后等于前一 committed envelope 的 `record_hash`。
- `payload_hash = SHA256(RFC8785(payload))`。
- `record_hash = SHA256(RFC8785(envelope_without_record_hash))`。
- datetime 进入 hash 前转为 UTC RFC 3339；非 canonical 对象必须在写文件前拒绝。

## 3. Canonical byte 格式

规范序列化使用 RFC 8785 UTF-8 bytes；frame/envelope 写文件时以该 bytes 加 `\n`。例如：

```json
{"actor":{"kind":"system","principal_id":null,"source":"taifeng","version":1},"attempt_id":null,"causation_id":null,"correlation_id":null,"occurred_at":null,"operation_id":"create_1","parent_record_id":null,"payload":{"config":{"model":"sim"}},"payload_hash":"<64-hex>","previous_hash":"0000000000000000000000000000000000000000000000000000000000000000","record_hash":"<64-hex>","record_id":"create_1:session_started","record_type":"session_started","recorded_at":"2026-07-21T08:00:00Z","schema_version":1,"seq":1,"session_id":"ses_1","submission_id":null,"thread_id":"thr_root","turn_id":null,"writer_epoch":1}
```

`<64-hex>` 在 conformance vector 中替换为真实固定值；尖括号示例不是合法持久化数据。

## 4. Batch frame

BEGIN/COMMIT 使用保留 key `__journal_frame__`，`frame_version=1`：

```json
{"__journal_frame__":"BEGIN","batch_id":"create_1:init","batch_payload_hash":"<64-hex>","expected_seq":0,"frame_version":1,"record_count":3}
{"actor":{"kind":"system","principal_id":null,"source":"taifeng","version":1},"attempt_id":null,"causation_id":null,"correlation_id":null,"occurred_at":null,"operation_id":"create_1","parent_record_id":null,"payload":{"config":{"model":"sim"}},"payload_hash":"<64-hex>","previous_hash":"0000000000000000000000000000000000000000000000000000000000000000","record_hash":"<64-hex>","record_id":"create_1:session_started","record_type":"session_started","recorded_at":"2026-07-21T08:00:00Z","schema_version":1,"seq":1,"session_id":"ses_1","submission_id":null,"thread_id":"thr_root","turn_id":null,"writer_epoch":1}
{"actor":{"kind":"system","principal_id":null,"source":"taifeng","version":1},"attempt_id":null,"causation_id":"create_1:session_started","correlation_id":null,"occurred_at":null,"operation_id":"create_1","parent_record_id":null,"payload":{"entry_skill_id":"general","extra":{},"source":"user","tags":[]},"payload_hash":"<64-hex>","previous_hash":"<record-1-hash>","record_hash":"<64-hex>","record_id":"create_1:thread_created","record_type":"thread_created","recorded_at":"2026-07-21T08:00:00Z","schema_version":1,"seq":2,"session_id":"ses_1","submission_id":null,"thread_id":"thr_root","turn_id":null,"writer_epoch":1}
{"actor":{"kind":"system","principal_id":null,"source":"taifeng","version":1},"attempt_id":null,"causation_id":"create_1:thread_created","correlation_id":null,"occurred_at":null,"operation_id":"create_1","parent_record_id":null,"payload":{"session_id":"ses_1","thread_id":"thr_root"},"payload_hash":"<64-hex>","previous_hash":"<record-2-hash>","record_hash":"<64-hex>","record_id":"create_1:thread_bound","record_type":"thread_bound","recorded_at":"2026-07-21T08:00:00Z","schema_version":1,"seq":3,"session_id":"ses_1","submission_id":null,"thread_id":"thr_root","turn_id":null,"writer_epoch":1}
{"__journal_frame__":"COMMIT","batch_id":"create_1:init","first_seq":1,"frame_version":1,"last_seq":3,"record_count":3,"records_hash":"<64-hex>","tail_hash":"<record-3-hash>"}
```

- BEGIN/COMMIT 不占 `seq`，也不成为 `previous_hash` 节点。
- `batch_payload_hash` 覆盖输入 `JournalRecord` fingerprints 的有序数组。
- `records_hash` 覆盖 committed envelope `record_hash` 的有序数组。
- reader 只有验证 frame identity、数量、连续 seq/hash 和 COMMIT 后才一次性发布 batch。
- EOF 位于 batch 内时 batch 全部不可见，tail 维持 BEGIN 前的 committed 值，health 为
  `RECOVERY_REQUIRED`。

## 5. Lease、ack 与 live 生命周期

```python
class SessionLease:
    session_id: str
    writer_id: str
    writer_epoch: int
    lease_id: str

class JournalAck:
    session_id: str
    first_seq: int
    last_seq: int
    record_ids: tuple[str, ...]
    tail_hash: str
    writer_epoch: int
    durability: Durability

class SessionCreateResult:
    lease: SessionLease
    ack: JournalAck
```

core 实例维护 per-session live writer cache。同一 `creation_operation_id`、writer id、descriptor
fingerprint 且 lease 仍 live 的同进程重试返回同一个 create result。其他 writer/operation/进程实例对已存在
文件只能收到 `JournalBusyError` 或 `JournalAlreadyExistsError`，不得复制旧 lease。

每次 append 必须完整匹配 session id、writer id、writer epoch、lease id。`close()` 在 per-session lock 内先把
writer 标记为 closed，再释放 lease/cache/文件资源；已经取得旧 writer 引用但仍排队等待 lock 的 append 也必须
拒绝。`close()` 不写 `session_ended`。跨进程接管与更高 epoch 由 Phase 2 负责。

## 6. 幂等与 CAS 顺序

同一 Session lock 内固定执行：

1. 校验 live lease 的 session id、writer id、writer epoch、lease id 全字段；无效 capability 不得触发
   scan，也不得观察 recovery/integrity 状态；
2. 拒绝 recovery-required 状态并 strict scan 当前物理 tail；
3. 查 committed record id；
4. 单条 fingerprint 相同返回原 ack，不看旧 `expected_seq`；不同抛 `JournalConflictError`；
5. batch 只有全部 id 属于同一原 batch、顺序和 fingerprint 相同才返回原 batch ack；部分重叠、跨 batch
   重组或内容不同均冲突；
6. 没有幂等命中时才要求 `expected_seq == committed_tail_seq`；
7. 生成并 durable commit 新 batch。

torn、未 committed batch 内出现同 record id 不算幂等成功；ordinary append 必须拒绝。

## 7. IO、取消与 durability

- open/write/flush/fsync/目录 fsync/scan 必须通过 `anyio.to_thread.run_sync`，不得阻塞 event loop。
- commit 入口前允许取消且不得写入；开始文件变更后用有界 shield 等待明确 ack 或异常。
- `COMMITTED` ack 只在 file flush+fsync 完成后返回；新建文件还必须 fsync 父目录。
- `SyncFileAdapter` 只有在明确证明目标尚未开始 mutation 时，才能用 `CommitNotStartedError` 包装原异常；
  该错误可透传并用原 CAS 重试。dispatch 后其余异常、取消和 timeout 一律按结果不确定处理。
- 结果不确定必须返回 `JournalRecoveryRequiredError(cause="commit_outcome_unknown")` 并冻结 live writer；
  recovery error 不得携带底层异常文本，后续 append 返回同一稳定 cause，且不得再次 dispatch。
- append_batch 返回一个覆盖完整 seq 范围的 `JournalAck`。

## 8. Strict load / verify

| 状态 | 结果 |
| --- | --- |
| clean committed tail | `JournalHealth.HEALTHY` + 精确 tail seq/hash |
| torn final physical line | `RECOVERY_REQUIRED` + 最后 committed tail |
| incomplete final batch | `RECOVERY_REQUIRED` + BEGIN 前 committed tail |
| malformed middle line | `JournalIntegrityError` |
| non-canonical physical JSON / duplicate key / schema type coercion | `JournalIntegrityError` |
| seq gap / duplicate | `JournalIntegrityError` |
| payload/previous/record hash mismatch | `JournalIntegrityError` |
| conflicting committed record id | `JournalIntegrityError` |

`load(after_seq=N)` 只返回 `seq > N` 的 committed envelope，保持原顺序。新 reader 禁止复用 legacy transcript
“跳过损坏行”策略。health 为 `RECOVERY_REQUIRED` 时 ordinary append 必须拒绝。

## 9. 验收

- canonical/hash vectors 覆盖 key 顺序、Unicode、`1.0 → 1`、非有限数字与非法对象。
- batch 覆盖 valid、missing COMMIT、frame hash mismatch、物理 JSON canonical/schema strictness 和
  kill-window 可见性。
- append 覆盖 caller nested mutation、batch precommit snapshot、ack-loss retry 仍先校验完整 live lease、
  record conflict、partial overlap、stale expected seq 与 stale lease。
- lifecycle 覆盖 create 首次 await 前输入快照，以及 close 先排队时拒绝已取得旧 writer 引用的 append。
- IO 覆盖 mutation 前取消、mutation 后取消/异常、slow fsync 不阻塞 event loop，以及 COMMIT 已写入后
  fsync error 触发 recovery-required。
- strict verify 覆盖本契约第 8 节全部状态。
- focused tests、全量 pytest、real-LLM selfcheck 与 capability matrix/ledger 刷新全部通过后，Phase 1 才可完成。
