# ADR 0008: 持久化层三协议拆分 + stdlib SQLite 默认索引

- 状态：Accepted
- 日期：2026-05-24
- 关联 change：`docs/architecture/conversation.md`

## 背景

`JsonlMessageStore`（`conversation/transcript.py`）原先把三类职责耦合在单一类里：

1. 消息体追加写（JSONL）
2. thread 元数据查询（SQLite `threads` 表）
3. 按 cwd / 时间 过滤排序（Python + SQL）

这套在小规模 / 单测试场景可行，到中等规模出现两个矛盾：

- **JSONL 全量扫描**：启动期或 `list_threads` 时扫所有 thread 文件，10k+ thread 后启动慢到不可接受
- **职责混合阻塞演进**：业务想接 PG / ES 高级检索无 plug-in 点；想加审计 / metrics 投递无事件 hook

业务方典型反馈：「为什么不直接传入 Redis / PG 配置？」/「taifeng 是库，业务应集中精力做业务」。

## 决策

把 `MessageStore` 拆为三个独立协议：

| 协议 | 职责 | 默认实现 | 业务可替换 |
| --- | --- | --- | --- |
| `MessageWriter` | 消息体 source-of-truth（append-only） | `JsonlMessageWriter` | 是（罕见） |
| `ThreadDirectory` | thread 元数据列表 / 查询 / 分页 | `SqliteThreadDirectory` | 是（常见） |
| `IndexHook` | thread 生命周期事件订阅（fire-and-forget） | `NoopIndexHook` | 是（常见） |

主路径默认：JSONL 主存 + stdlib SQLite 索引 + Noop hook。业务无需任何代码即可获得完整能力。

四档配置光谱：

```python
# A. 零配置（覆盖 80% 业务）
engine = AgentEngine(storage_dir=Path("./data"))

# B. 业务接管索引（推荐扩展点）
engine = AgentEngine(storage_dir=..., thread_directory=MyRedisDirectory(...))

# C. 显式不要索引（嵌入式）
engine = AgentEngine(storage_dir=..., thread_directory=NullThreadDirectory())

# D. 业务自接主存（极少见，需自实现 append-only + 并发 + replay）
engine = AgentEngine(message_writer=MyPostgresWriter(...), thread_directory=MyPostgresDirectory(...))
```

## 备选方案

### A. 拒绝在 src 内任何 SQLite —— 用 `index.jsonl` tail-read + 内存 LRU 兜底

最初的 proposal 走这条路，被推翻：

- `sqlite3` 是 Python 标准库，零依赖成本；R1 红线本质是禁第三方业务客户端，不是禁 stdlib
- index.jsonl 大量随写涨字节，无索引情况下 list 查询 O(N) 扫描；20k+ thread 启动后明显慢
- 把"自带索引"的责任丢给业务侧会催生一堆半坏实现 —— 与「库责任」相悖
- 参照对象 codex 自己也用 SQLite (`~/.codex/sessions/`)

### B. 内置 Redis / PG 作为默认实现

拒绝：

- 引入第三方依赖（redis-py / asyncpg / SQLAlchemy）—— 直接违反 R1
- 业务方往往已有自己的 connection pool / retry / 多区域路由 / OpenTelemetry，库再起一个 client 会冲突
- DSN 格式 / pgbouncer / Aurora / 国产 Tair / SSL 证书路径 千差万别，库 hard-code 永远填不满业务参数
- taifeng 自己的 CI 必须起 Redis / PG container 跑集成测试

### C. 接受 Redis / PG 连接 URL 参数，src 内构造 client

拒绝：

- 看似简单，半年后会被业务追着加 `redis_pool_size / redis_socket_timeout / redis_sentinel_hosts / redis_cluster_mode / pg_pgbouncer_compatible / pg_ssl_mode / ...` 几十个参数
- 客户端选型被锁死（redis-py vs aioredis vs valkey；asyncpg vs psycopg）
- 业务无法注入已有 pool / tracing / 多租户路由策略
- 同样需要在 pyproject 加可选依赖，未真正避免依赖污染

### D. 完全 IoC —— 连默认 SQLite 都丢给业务

拒绝：

- 库会从「装上就能跑」退化为「先实现 3 个 Protocol 才能跑」 —— 从库变 framework
- demo / 教程必须附带 30+ 行 storage 代码
- 多数用户根本不需要多机部署，stdlib SQLite + WAL 已足够

## 影响

### 公共 API

- 保留 `JsonlMessageStore` 作为兼容封装（`JsonlMessageWriter + SqliteThreadDirectory + NoopIndexHook`），旧业务代码零改动
- `EnginePool.create` 新增可选参数 `storage_dir / thread_directory / index_hook / sink`
- `taifeng.__init__` 新增导出三协议 + 4 个 frozen dataclass + 默认实现 + `rebuild_index`

### 红线措辞调整

- **R1 业务零侵入**：放宽允许 `import sqlite3`，**严格限定**于 `src/taifeng/conversation/sqlite_directory.py` 一处（T9 grep 强制）；继续禁止 redis-py / asyncpg / sqlalchemy / psycopg / aiosqlite / aioredis / 业务术语 / 业务字段
- **R3 可观测**：新增 8 个 EventMsg variants 覆盖持久化层全部异常路径（`transcript_skipped_corrupt_line / sqlite_schema_rebuilt / sqlite_db_corrupt_rebuilt / thread_indexed_orphan / directory_cursor_reset / index_hook_failed / index_hook_abandoned / rebuild_skipped_corrupt`）
- **R5 可 resume**：JSONL 主存永远是 source-of-truth；SQLite 索引丢失 / 损坏 / schema 升级均能从 JSONL 通过 `rebuild_index` 完整重建，永不需要 alembic 之类的 migration 工具

### Schema 演化策略

- SQLite 表里有 `schema_meta(version INTEGER)` 行
- taifeng 启动期版本不匹配 → drop 所有表 + 调用 `rebuild_index` 从 JSONL 全量重建 + 发 `sqlite_schema_rebuilt` 事件
- 永远不写 migration 脚本；代价是「启动多 N 秒重建」，明确文档

### 旧数据兼容

- 现有用户 JSONL 数据保留在原来位置；新 `JsonlMessageWriter` 改用 flat 布局 `<threads_dir>/<thread_id>.jsonl`
- 旧 date-bucketed 布局 `<threads_dir>/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl` 不被新 writer 识别
- 升级后访问旧 thread 需手动跑迁移脚本（待 T8 后补；MVP 接受首次升级丢老 thread 索引，主消息内容仍在）

## 验证

- 177 个测试全绿（含 60+ 新增持久化层测试）
- R1 grep 验证：`import sqlite3` 仅在 `sqlite_directory.py`；其它第三方 DB 客户端无 import
- examples/observability/audit_index_hook.py 端到端可跑（3 个 turn → 7 行 audit log）
- examples/{redis,postgres}_thread_directory.py 骨架可 import
