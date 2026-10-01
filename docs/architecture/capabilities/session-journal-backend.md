# Capability: session-journal-backend（SessionJournal 后端 seam）

> 状态：🧪 Experimental（随审计模式一起）。决策记录：[ADR 0114](../../decisions/0114-session-journal-backend-seam.md)。
> 入口在 `taifeng.experimental`，一致性检查在 `taifeng.testing`。core 的行为语义见
> [session-journal-core](session-journal-core.md)，本篇只讲「怎么换后端」。

## Purpose

审计模式下 SessionJournal 是会话的唯一事实源。默认后端是本机文件（`JsonlSessionJournalCore` + `flock`），
多实例部署需要的是能跨机器共享的 Journal 与跨机器的写者互斥。本契约把后端做成可替换的，并给出验收
外部实现的一致性检查：

| 做法 | 实现什么 | 内核替你做的 | 代价 |
| --- | --- | --- | --- |
| **换存储** | `SyncFileAdapter`（3 个同步方法）+ `WriterLockAdapter`（2 个），注入 `JsonlSessionJournalCore` | hash chain、batch 帧、幂等、接管、strict verify、结果未知的冻结 | core 每次追加整读一遍该 Session 的 Journal 做 strict scan |
| **换 core** | `SessionJournalCore`（5 个异步方法），注入 `AuditConfig.journal_core` | 只给存储无关的构件 | 自己保证事务、互斥与故障语义；追加可以与 Journal 长度无关 |

## 数据契约

### 换存储

```python
class SyncFileAdapter(Protocol):          # 同步；core 统一派发到线程池
    def create_exclusive(self, path: Path, payload: bytes) -> None: ...
    def read_bytes(self, path: Path) -> bytes: ...
    def append_durable(self, path: Path, payload: bytes) -> None: ...

class WriterLockAdapter(Protocol):        # 同步；非阻塞
    def acquire(self, path: Path) -> object: ...   # 被占 → WriterLockBusyError
    def release(self, handle: object) -> None: ...

JsonlSessionJournalCore(root, sync_file_adapter=..., writer_lock_adapter=...)
```

- `path` 是键：Journal 为 `<root>/<session_id>.journal.jsonl`，锁为 `<root>/<session_id>.journal.lock`；
  非文件后端取 `path.name` 即可。
- `create_exclusive` 已存在时 SHALL 抛 `FileExistsError`（可包在 `CommitNotStartedError` 里），原内容不变。
- `read_bytes` 不存在 SHALL 抛 `FileNotFoundError`。
- `append_durable` 返回即表示 durable；追加 SHALL 按调用顺序累积，对其他实例可见。
- 能证明「还没有开始改动」的失败 SHOULD 用 `CommitNotStartedError(原异常)` 包装——core 据此判定可以安全
  重试；其余异常、超时、取消一律按「提交结果未知」处理，冻结该 writer。
- `acquire` SHALL 非阻塞：被别的实例（或同一实例）持有时立即抛 `WriterLockBusyError`。持有者的进程消失
  后锁 SHALL 能被别人取得（租约到期、连接断开即释放等），否则崩溃的 Session 无法被接管。

### 换 core

```python
class SessionJournalCore(Protocol):
    async def create_session(self, descriptor: SessionDescriptor) -> SessionCreateResult: ...
    async def open_existing(self, session_id, *, writer_id, operation_id) -> SessionOpenResult: ...
    async def append_batch(self, records, *, lease, expected_seq) -> JournalAck: ...
    async def close_session(self, lease: SessionLease) -> None: ...
    def load(self, session_id, *, after_seq=0) -> AsyncIterator[JournalEnvelope]: ...
```

签名里的类型（`SessionDescriptor` / `RootThreadDescriptor` / `ActorRef` / `JournalRecord` / `JournalEnvelope` /
`SessionLease` / `JournalAck` / `SessionCreateResult` / `SessionOpenResult` / `WriterTakeoverV1`）与错误类型
（`JournalBusyError` / `JournalAlreadyExistsError` / `JournalConflictError` / `JournalLeaseError` /
`JournalSessionNotFoundError` / `JournalSessionEndedError` / `JournalIntegrityError` /
`JournalRecoveryRequiredError`）都从 `taifeng.experimental` 导出。

存储无关的构件（纯函数，不做 IO）：

| 构件 | 用途 |
| --- | --- |
| `build_initialization_records(descriptor)` | 创建时的三条记录 |
| `build_takeover_record(...)` | 接管记录 |
| `snapshot_records(records)` | 在任何 await 之前固定调用方输入并算 fingerprint |
| `seal_batch(records, previous_seq=, previous_hash=, writer_epoch=)` → `SealedBatch(envelopes, fingerprints, ack)` | 分配 seq、接 hash chain、给出 ack；`SealedBatch.committed()` 是并入幂等索引的条目 |
| `resolve_idempotent_ack(records, fingerprints, lookup)` | 判定是否完整重试：返回原 ack、`None`（新内容）或抛 `JournalConflictError` |
| `verify_envelopes(envelopes, session_id=)` | 从 seq 1 起 strict 校验整条链（seq、hash、epoch 只经接管单步递增） |
| `index_envelopes(envelopes, batch_acks)` | 从已提交内容重建幂等索引 |
| `ended_record_id(envelopes)` / `descriptor_fingerprint(descriptor)` / `record_fingerprint(record)` / `ZERO_HASH` | 终结判定、创建请求与 record 的 fingerprint、链的起点 |

`InMemorySessionJournalCore(storage: InMemoryJournalStorage)` 是只用上述构件写成的参考实现：数据库版后端
照它的形状写——`create_session` / `append_batch` / `open_existing` 各是一个事务，「读状态 → 判定 → 写入」
不可被别的事务插入。它不 durable，只用于测试与当样板。

## 行为契约

### Requirement: 后端以一致性检查验收

外部后端 SHALL 通过 `taifeng.testing.journal_core_cases()` 的全部检查才可注入 `AuditConfig.journal_core`。
换存储的后端把适配器装进 `JsonlSessionJournalCore` 后跑同一套检查；另有
`journal_storage_cases()` / `writer_lock_cases()` 直接检查两个适配器（定位问题更直接）。

```python
from taifeng.testing import journal_core_cases

class Harness:                      # 一个 harness = 一份全新的、互不相干的存储
    async def new_core(self): ...   # 接在这份存储上的新实例（相当于另一个进程）
    async def abandon(self, core): ...   # 模拟该进程消失：释放它的 writer，不写记录

@pytest.mark.parametrize("case", journal_core_cases(), ids=lambda case: case.name)
async def test_journal_core(case, tmp_path):
    await case.run(Harness(tmp_path))
```

检查不依赖测试框架：失败抛 `ConformanceFailure`（`AssertionError` 子类，文本写明期望与实际）；
`run_cases(cases, harness_factory)` 返回 `{case 名: 失败原因}`。

core 检查 22 项，覆盖：创建写下初始化三记录且可幂等重试、已存在即拒绝；追加的连续 seq 与 hash chain、
调用方字段原样保留、整批原子与顺序、`expected_seq` CAS、完整重试返回原 ack、内容不同 / 部分重叠 /
重组即冲突、lease 全字段匹配、坏批次在写入前被拒、并发追加恰好成功一个；`close_session` 之后不可写且可
被接管；writer 存活时第二个 writer 被拒且不留痕迹；接管 epoch + 1 并记录接管前的尾、可幂等重试、结果丢失
后重试复用已落库的接管记录、该 epoch 已被使用则冲突、新 writer 可写而旧 lease 失效、接管前已提交内容的
幂等索引延续；已终结的 Session 不可再开；读取的顺序与 `after_seq`；Session 之间互不相干。

#### Scenario: 检查能查出有缺陷的后端
- **WHEN** 一个不做 `expected_seq` CAS 的后端、一个接管时不要求原 writer 已释放的后端、一个追加没有
  事务的后端、一个读取不按 seq 顺序的后端分别跑这套检查
- **THEN** 各自在对应的 case 上失败，失败文本写明期望的错误类型

### Requirement: 内核对 core 没有协议之外的依赖

`AuditConfig.journal_core` SHALL 接受任何满足 `SessionJournalCore` 的对象；内核不检查它的具体类型。

#### Scenario: 审计会话跑在非默认后端上
- **WHEN** 审计 Session 注入 `InMemorySessionJournalCore`，或注入装了外部存储 / 锁适配器的
  `JsonlSessionJournalCore`，经历「工具调用处停下等审批 → 释放 → 另一个实例接管 → `Resume` → 终结」
- **THEN** 会话照常完成，Journal 里有 `session_detached`、`writer_takeover`、`session_ended`
- **AND** 整条链经 `verify_envelopes` 校验通过，本机目录下没有 Journal 文件

## 能力边界（如实记录）

- 通用检查不覆盖**物理损坏与结果未知的提交**（torn tail、写到一半的 batch、fsync 失败后的冻结）：它们取决于
  存储介质。JSONL core 自己的测试覆盖本机文件的这些情形；换 core 的后端须自测并保证提交结果未知时不再
  接受该 writer 的追加（否则可能在未知状态上继续写）。
- 「换存储」的 core 每次追加都整读并 strict scan 整个 Session 的 Journal：适合会话不长、或存储读取很快的
  场景；长会话 + 远端存储请「换 core」。
- 审计模式的对话投影仍只写默认的 `JsonlMessageStore`（ADR 0111）：共享 Journal 之后，接管的实例从 Journal
  重建 history，投影文件是各节点本地的兼容视图。
- 旧 transcript 导入（`import_legacy_transcript`）目前只接受 `JsonlSessionJournalCore`。

## R1–R5 影响

- R1：无业务概念。R2–R4：无影响。
- R5：审计 Session 的接管不再要求共享卷——换成共享后端即可由另一台机器上的实例接管。
