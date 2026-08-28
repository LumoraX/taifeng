# 对话持久化（store-protocol-decoupling 重写）

> §1.3 —— `MessageWriter` + `ThreadDirectory` + `IndexHook` 三协议分离；默认 JSONL 主存 + stdlib SQLite 索引 + Noop 事件钩子。

## 设计目标

- **JSONL 主存 = source-of-truth**（R5 强制）：append-only，进程崩溃后可 resume
- **索引可重建（derived）**：SQLite 丢失 / 损坏 / schema 升级均能从 JSONL 自愈
- **业务可正交替换**：换索引（Redis / PG）不影响主存；接事件订阅（审计 / ES）不影响索引
- **零配置开箱即用**（避免库变 framework）：传 `storage_dir` 一个参数即拿到完整能力
- **不引第三方 DB 依赖**（R1 强制）：src 内仅允许 stdlib `sqlite3`

## 实验性 SessionJournal durable core（Phase 1）

`taifeng.conversation.journal` 现包含一个隔离的、实验性的 Session 级 durable core。它使用 RFC 8785
canonical JSON、SHA-256 envelope hash chain、`BEGIN + envelopes + COMMIT` 原子 batch、同进程 live lease
fencing，以及 fail-closed strict verification。每次 durable ack 都在 file flush+fsync 后返回；新建文件还会
fsync 父目录。同步文件操作和 strict scan 全部经 anyio worker thread 执行。

这一能力当前**不是默认 conversation 持久化路径**（legacy 模式）：

- 未注入 `AuditConfig` 时，`AgentEngine`、`EnginePool`、`MessageWriter` / `MessageStore` 和 EventMsg
  **不接入** SessionJournal；现有 per-thread JSONL transcript、resume、reconstruct 和 corrupt-line
  tolerance 行为逐字不变，SessionJournal 不与它双写，也不改变其公共 API。

### audit-required Session（business-integration，已接入）

注入 `AuditConfig` 后，EnginePool 为该 Session 建立 **Journal-first 审计执行模式**：Journal 成为该 Session
的**授权真相源**，per-thread JSONL transcript 降级为**只读投影目标**（projection target），由
`JournalConversationProjector` 仅消费已 durable ack 的 `conversation_item` envelope 单调物化——投影失败只
返回 stale、不冻结 Journal 执行，replay 按 Journal seq 可重建同序历史。

- **Journal authority**：UserMessage、LLM attempt/response、Tool intent/outcome、同步 call_skill 子谱系、
  会话项（user/assistant/reasoning/function_call/function_call_output/skill_outcome）全部先 durable 落
  Journal，ack 后才应用到 hot history 与 projection；效果永远后于 durable。
- **lifecycle**：`SessionAuditCoordinator` 持单 Session 的 append/lifecycle 锁、OPEN/FINISHING/CLOSED 生命周期、
  一个 finish future、terminal batch（thread terminal + 唯一 `session_ended`）+ `close_session(lease)` 恰一次；
  首个 Journal IO / 完整性 / ack 不确定失败即关闭 effect gate（freeze），且**每 Session 独立**——一个
  Session 冻结不影响其他 Session。
- **current recovery exclusions（本阶段不支持）**：audit 静态拒绝 resume、custom store/directory、IndexHook、
  hooks、permission/HITL、compressor、memory、instruction layers、orchestration、spawn/peer、非 attempt-
  observable client、可 suspend / metadata 不全的 Tool；能力面外的动态 Op 在 submission gateway 前 durable
  拒绝。跨进程崩溃接管、repair/reconcile/unfreeze、历史迁移仍不在本阶段范围。

完整数据契约与边界以
[SessionJournal Business Integration 能力契约](capabilities/session-journal-business-integration.md)、
[SessionJournal Durable Core（Phase 1）能力契约](capabilities/session-journal-core.md) 和
[ADR 0025](../decisions/0025-session-journal-source-of-truth.md) 为准。

## 三协议总览

```
                  ┌─────────────────────────────────────────┐
                  │            EnginePool / Engine          │
                  └─────────────────────────────────────────┘
                       │              │              │
              ┌────────▼─────┐  ┌─────▼──────┐  ┌────▼──────┐
              │ MessageWriter│  │ThreadDirectory│ │ IndexHook │
              │ (主存写读)   │  │  (元数据)    │  │ (事件订阅)│
              └────────┬─────┘  └─────┬──────┘  └────┬──────┘
                       │              │              │
              ┌────────▼─────┐  ┌─────▼──────────┐ ┌▼──────────┐
              │JsonlMessage  │  │SqliteThread    │ │NoopIndex  │
              │Writer (默认) │  │Directory (默认)│ │Hook (默认)│
              └──────────────┘  └────────────────┘ └───────────┘
                                          ↑              ↑
                                          │              │
                                可替换：业务侧实现协议即注入
                                Redis / PG / ES / Audit / Kafka
```

## 数据布局

```
<storage_dir>/
├── threads/
│   ├── thr_abc123...jsonl          # 一个 thread 一个 JSONL 文件（flat 布局）
│   │   ├── 首行 {"__meta__": true, thread_id, created_at, ...}
│   │   ├── {"kind": "user_message", ...}
│   │   ├── {"kind": "assistant_message", ...}
│   │   └── ...
│   └── thr_def456...jsonl
└── taifeng-index.db                # SQLite 索引（derived，可重建）
```

**主存**：每行单 ResponseItem 的 JSON 序列化 + 单次 `write()`（POSIX 4KB 原子）。

> **reasoning 落史**（reasoning-content-passback）：thinking 模型采样产生 `reasoning_delta` 且该轮有产出（assistant 文本或 tool_calls）时，累积全文落一条 `kind="reasoning"` item，**紧邻其配对 assistant_message 之前**（与 provider 产出顺序一致）。该 item 供 prompt 重建时回传 `reasoning_content`（thinking 模型续传契约，见 `llm-client.md` reasoning 回传节）；非 thinking 模型不产生该 kind，零变化。
**索引**：`thread` 表存 ThreadMetadata 行；`schema_meta` 表存版本号；WAL + `synchronous=NORMAL`。

## MessageWriter 协议

```python
class MessageWriter(Protocol):
    async def create_thread(*, entry_skill_id, source="user", tags=(), extra=None) -> str: ...
    async def append(thread_id, items: list[ResponseItem]) -> None: ...
    async def load_history(thread_id) -> list[ResponseItem]: ...
```

默认 `JsonlMessageWriter`：

- 首行写 `__meta__` 行让 thread 自包含元数据（`rebuild_index` 复原源头）
- append 不加跨进程锁，依赖 POSIX 4KB 原子保证；并发不撕裂
- load_history 跳过首行 metadata + 跳过损坏行（发 `transcript_skipped_corrupt_line` 事件）

## ThreadDirectory 协议

```python
class ThreadDirectory(Protocol):
    async def list_threads(*, limit=50, cursor=None, filter=None) -> ThreadPage: ...
    async def get_metadata(thread_id) -> ThreadMetadata | None: ...
    async def update_metadata(thread_id, patch) -> None: ...
    async def upsert_metadata(meta) -> None: ...
```

默认 `SqliteThreadDirectory`（src 内**唯一** `import sqlite3`）：

- 启动期 `schema_meta` 版本不匹配 → drop 所有表 + 调 `rebuild_index` + 发 `sqlite_schema_rebuilt`
- 启动期 `PRAGMA integrity_check` 损坏 → rename 备份 + 重建 + 发 `sqlite_db_corrupt_rebuilt`
- 所有 async 方法通过 `anyio.to_thread.run_sync` 派发，不阻塞 event loop
- 单 connection + `anyio.Lock` 串行化所有调用
- 复合 cursor `(updated_at, thread_id)` base64 编码；损坏 → 重置 + `directory_cursor_reset` 事件
- list_threads 时 `<threads_dir>/<thread_id>.jsonl` 不存在 → 跳过 + `thread_indexed_orphan` 事件

可替换实现：

- `NullThreadDirectory` —— 嵌入式场景显式关索引
- `RedisThreadDirectory` —— `examples/persistence/redis_thread_directory.py` 骨架
- `PostgresThreadDirectory` —— `examples/persistence/postgres_thread_directory.py` 骨架

## IndexHook 协议

```python
class IndexHook(Protocol):
    async def on_thread_created(meta) -> None: ...
    async def on_message_appended(thread_id, items) -> None: ...
    async def on_metadata_updated(thread_id, patch) -> None: ...
```

调用模型：

- 主路径写完成 → spawn background task 调 hook，不阻塞 turn
- hook 抛异常 → 发 `index_hook_failed`（含 method / thread_id / cause），主路径不受影响
- engine.shutdown → await pending hooks 最多 5s grace → 超时 cancel + 发 `index_hook_abandoned`
- 构造期 Protocol 校验：缺方法 → raise `TypeError`

业务用途：审计日志 / metrics / 异步投递 ES / Kafka。**与 ThreadDirectory 正交**：业务可同时换索引 + 加事件钩子。

## rebuild_index 工具

```python
report = await rebuild_index(writer, directory, *, dry_run=False, sink=None)
# RebuildReport(scanned_count, indexed_count, orphan_count, error_count, elapsed_ms)
```

> `orphan_count` 在 rebuild 路径恒为 0 —— rebuild 以 JSONL 为源逐文件重建，不存在"索引有、文件无"的孤儿；
> 孤儿检测发生在 `SqliteThreadDirectory.list_threads`（JSONL 缺失时跳过 + 发 `thread_indexed_orphan`），不在此。

用途：

- SqliteThreadDirectory schema 升级内部调（自动从 JSONL 重建索引）
- 业务侧 Redis / PG 索引丢失后从 JSONL 恢复
- 数据迁移：换 ThreadDirectory 实现时初次填充

损坏首行 → 计入 error_count + 发 `rebuild_skipped_corrupt`。

## 何时换什么

| 业务需求 | 实现什么 | 估计 LOC |
| --- | --- | --- |
| 单机 / 中小规模 | 用默认 | 0 |
| 加速 list / 多机共享元数据 | `ThreadDirectory` (Redis/PG) | ~30-80 |
| 审计 / 投递 ES / Kafka / 异步 metric | `IndexHook` | ~10-50 |
| 主存必须落 PG / 合规要求 | `MessageWriter` + `ThreadDirectory` | ~150-300 |

## reconstruct_logical_history（冷加载逻辑 history 重建）

图片 user item 把完整 `ImageAttachmentV1` 作为 canonical JSON 保存（MIME、decoded size、SHA-256、裸 base64、detail）；不保存 provider Data URL。冷加载后 prompt 层重新执行 admission，因此磁盘内容被篡改、策略收紧或换到 text-only client 时都会在网络前 fail closed。

OpenAI Responses 的一个 terminal sample 通过 `append_atomic_batch(items, batch_id=llm_sample_id)` 写为 begin/items/commit frames。reader 只发布 digest 与 item ids 完整匹配的首个 commit；崩溃留下的半 batch 不可见，同 batch 同 digest 幂等，不同 digest 报 conflict。默认 JSONL 的普通 append 与原子 batch 共用 `<thread>.lock` advisory file lock，committed 检查和 durable append 在同一跨 writer 临界区；文件读写、flush/fsync 和阻塞锁调用均在 anyio worker thread，不阻塞主 actor。

reasoning provider state、function call 和后续 `origin_llm_sample_id` 工具结果保持有序，跨进程只需显式 `resume_thread_id` 即可重建，不依赖 provider conversation id。冷恢复若发现已 commit 的 Responses function call 没有 matching output，且它不属于活跃 suspension，则用稳定 item id 与 recovery batch 追加 `is_error=True` 的 `tool outcome unknown after process recovery; not retried`。该路径只收敛未知结果，绝不重新 dispatch 工具；`thread_resumed.recovered_unknown_call_ids` 暴露本次收敛的 call ids。

`src/taifeng/conversation/reconstruct.py` 提供纯函数 `reconstruct_logical_history(raw)`，把 append-only transcript（`load_history` 返回的原始序列）重放成与热内存等价的逻辑 history。纯 CPU、无 IO。对干净 thread（无压缩、无 rewind）是恒等映射，向后兼容。

**为什么需要它**：append-only 主存在两种情况下与热内存 history 发散：

1. **压缩**：`SlidingWindow` / `Handoff` 把内存 history 替换成 `[head, placeholder, (salvage_note), tail]`，但只把 `compacted` placeholder **追加到 JSONL 末尾**，被替换的中间项**不删**（R5 append-only）。直接读 raw 会得到 `[head, middle(废弃), tail, placeholder@末尾]`，与内存结构性发散。
2. **历史 rewind / rollback**：截断只动内存，被截掉的项仍留在 store，其后是 `rewind` / `rollback` marker + re-run 项。直接读 raw 会包含废弃尾部。

**重放规则（按写入序扫一遍）**：

| item kind / source | 动作 |
| --- | --- |
| `system_injection`，`source == memory_pre_evict`（压缩 salvage digest） | 暂存，等下一个 `compacted` 时挪到 placeholder 之后（复现热内存 `insert_at = summary_index + 1` 行为） |
| `compacted`（带 `replaced_range=(s, e)`） | 把 `logical[s:e]` 折叠掉：`logical = logical[:s] + [placeholder] + ([salvage] if salvage else []) + logical[e:]` |
| `system_injection`，`source ∈ {rewind, rollback}` | 截断信号：`logical = logical[:cut_index]`（`cut_index` 从 payload 读），**marker 本身不进 logical** |
| `skill_outcome`（战绩旁路记账） | `logical.append(item)`（正常追加，保留在 logical history 供后续相位读取）；但 `build_api_request` 在构建 LLM 消息序列时**跳过**此 kind——旁路语义，不进 LLM 视图 |
| 其余所有 item | `logical.append(item)` |

**副作用：修正既存 resume 隐患**。冷加载（`initial_history`）和 `pool.py` resume 路径均改为先 `reconstruct_logical_history`，使压缩过的 thread resume 后**不再把废弃项重发给 LLM**（原先 resume 不崩故未被发现，但会在下次 pre-turn 重压前多发一轮废弃上下文）。

**依赖契约（R5）**：`reconstruct` 依赖 `MessageStore.load_thread` 按写入顺序、完整吐回所有 `ResponseItem`（不去重 marker、不丢、不乱序）。默认 `JsonlMessageStore` 天然满足；自实现 DB store 时为协议红线。

## 关键决策与备选方案

详见 ADR `docs/decisions/0008-store-protocol-decoupling.md`。

## 红线影响

- **R1 业务零侵入**：放宽允许 `import sqlite3`，限定唯一文件；继续禁第三方 DB 客户端
- **R3 可观测**：新增 8 个 EventMsg variants（`transcript_skipped_corrupt_line / sqlite_schema_rebuilt / sqlite_db_corrupt_rebuilt / thread_indexed_orphan / directory_cursor_reset / index_hook_failed / index_hook_abandoned / rebuild_skipped_corrupt`）
- **R5 可 resume**：JSONL 永远是 source-of-truth；任意 directory 实现丢失数据均能从 JSONL 恢复
