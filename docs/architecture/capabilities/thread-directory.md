# thread-directory Specification

## Purpose
TBD - created by archiving change store-protocol-decoupling. Update Purpose after archive.
## Requirements
### Requirement: 数据契约 ThreadMetadata / ThreadFilter / ThreadPage

系统 SHALL 提供如下 frozen dataclass 作为协议的输入输出类型：

- `ThreadMetadata`：`thread_id: str` / `created_at: float` / `updated_at: float` / `entry_skill_id: str` / `source: str` / `tags: tuple[str, ...]` / `extra: dict`
- `ThreadFilter`：`entry_skill_id: str | None = None` / `created_after: float | None = None` / `created_before: float | None = None` / `tag: str | None = None` / `source: str | None = None`
- `ThreadPage`：`items: list[ThreadMetadata]`（按 updated_at 倒序）/ `next_cursor: str | None`（None 表示已到末尾）

#### Scenario: dataclass 不可变
- **WHEN** 代码尝试 `meta.updated_at = 999.0`
- **THEN** 系统 SHALL raise `dataclasses.FrozenInstanceError`

#### Scenario: 从 taifeng 顶层导入
- **WHEN** 业务侧 `from taifeng import ThreadMetadata, ThreadFilter, ThreadPage`
- **THEN** 三个符号 SHALL 全部可用

### Requirement: ThreadDirectory 协议形状

系统 SHALL 提供 `ThreadDirectory` 作为 `typing.Protocol` 且 `runtime_checkable`，方法签名固定为：

- `async def list_threads(*, limit: int = 50, cursor: str | None = None, filter: ThreadFilter | None = None) -> ThreadPage`
- `async def get_metadata(thread_id: str) -> ThreadMetadata | None`
- `async def update_metadata(thread_id: str, patch: dict) -> None`
- `async def upsert_metadata(meta: ThreadMetadata) -> None`

#### Scenario: 任意实现可通过 isinstance 校验
- **WHEN** 业务侧自定义类实现以上 4 个 async 方法
- **THEN** `isinstance(instance, ThreadDirectory)` SHALL 返回 True（runtime_checkable）

#### Scenario: 实现缺方法在使用时报错
- **WHEN** 业务侧传入缺少 `upsert_metadata` 的对象给 EnginePool
- **THEN** Engine SHALL 在构造时 raise `TypeError`

### Requirement: list_threads 排序与分页

`directory.list_threads(limit=N, cursor=...)` 返回的 items SHALL 按 `updated_at` 倒序（最新优先），长度 SHALL ≤ limit；若还有更多结果，`next_cursor` SHALL 非空，否则 SHALL 为 None。

#### Scenario: 100 个 thread 翻 10 页拿完
- **WHEN** 100 个 thread 已存在，反复调用 `list_threads(limit=10)` 并把上一次的 next_cursor 传入下一次
- **THEN** 10 次调用 SHALL 拿到全部 100 个 thread，无重复无遗漏
- **AND** 第 10 次调用返回的 next_cursor SHALL 为 None

#### Scenario: 损坏 cursor 重置从头开始
- **WHEN** cursor 字符串无法被实现解析（base64 错误 / 字段缺失 / 越界等）
- **THEN** directory SHALL 从头开始返回首页结果，不抛异常
- **AND** SHALL 发 EventMsg `directory_cursor_reset`（含原 cursor 字符串）

### Requirement: 过滤组合应用 AND 语义

当 ThreadFilter 中多个字段非空，系统 SHALL 仅返回所有非空条件都满足的 thread（AND 语义）。

#### Scenario: entry_skill_id + tag 组合
- **WHEN** filter = `ThreadFilter(entry_skill_id='x', tag='production')`
- **THEN** 仅 entry_skill_id == 'x' 且 'production' ∈ tags 的 thread SHALL 出现在结果中

#### Scenario: 时间窗 + source 组合
- **WHEN** filter = `ThreadFilter(created_after=T1, created_before=T2, source='subskill:foo')`
- **THEN** 仅 created_at ∈ (T1, T2) 且 source == 'subskill:foo' 的 thread SHALL 出现在结果中

### Requirement: update_metadata 部分合并

`directory.update_metadata(thread_id, patch)` SHALL 把 patch 字典合并到现有 metadata（last-write-wins，patch 未提及字段保留），SHALL 自动更新 `updated_at = 当前时间`。

#### Scenario: 部分字段更新不影响其它字段
- **WHEN** 现有 metadata.extra = `{"a": 1, "b": 2}`，调用 `update_metadata(tid, {"extra": {"a": 99}})`
- **THEN** 后续 `get_metadata(tid)` 返回的 extra SHALL == `{"a": 99}`（整段替换 extra）
- **AND** thread_id / created_at / entry_skill_id / source SHALL 保持不变
- **AND** updated_at SHALL > 更新前的 updated_at

#### Scenario: 不存在的 thread_id 抛 ThreadNotFoundError
- **WHEN** 调用 `update_metadata("not-exist", {})`
- **THEN** 系统 SHALL raise `ThreadNotFoundError`

### Requirement: upsert_metadata 写入或整体替换

`directory.upsert_metadata(meta)`：若 thread_id 不存在 SHALL 插入新行；若已存在 SHALL 整体替换。主要由 `MessageWriter.create_thread` 与 `rebuild_index` 工具调用。

#### Scenario: 不存在则插入
- **WHEN** directory 中无 thread_id "T1"，调用 `upsert_metadata(meta_T1)`
- **THEN** 后续 `get_metadata("T1")` SHALL 返回 meta_T1

#### Scenario: 已存在则整体替换（与 update 部分合并不同）
- **WHEN** directory 中已有 "T1" 的某 metadata，调用 `upsert_metadata(new_meta_T1)`
- **THEN** 后续 `get_metadata("T1")` SHALL 返回 new_meta_T1，旧字段被整体覆盖

### Requirement: orphan 检测跳过 + 事件

list_threads 即将返回的 thread_id 如果在 MessageWriter 主存中不存在，directory SHALL 跳过该条并发 EventMsg `thread_indexed_orphan`（含 thread_id）。

#### Scenario: 主存文件被外部删除
- **WHEN** SQLite 索引中含 thread "T1"，但 `<threads_dir>/T1.jsonl` 已被外部脚本删除
- **THEN** `list_threads()` 结果 SHALL 不包含 T1
- **AND** SHALL 发 EventMsg `thread_indexed_orphan`（thread_id="T1"）

### Requirement: limit 越界 raise ValueError

当 `list_threads(limit=N)` 的 N < 1 或 N > 1000，系统 SHALL raise `ValueError`。

#### Scenario: limit=0
- **WHEN** 调用 `list_threads(limit=0)`
- **THEN** 系统 SHALL raise `ValueError`

#### Scenario: limit=1001
- **WHEN** 调用 `list_threads(limit=1001)`
- **THEN** 系统 SHALL raise `ValueError`

### Requirement: 底层存储错误归类为 DirectoryError

底层存储不可达 / IO 错误 / 锁争用超过重试 SHALL raise `DirectoryError`（含 `__cause__` 指向底层异常）。

#### Scenario: 索引文件所在磁盘只读
- **WHEN** SqliteThreadDirectory 写入时底层 sqlite3 抛 OperationalError("disk I/O error")
- **THEN** directory SHALL raise `DirectoryError`，且 `err.__cause__` SHALL 指向原 OperationalError

### Requirement: 默认 SQLite 实现自我修复 schema 不匹配

内置 SqliteThreadDirectory 启动时若检测到内部 schema 版本与当前代码版本不匹配，SHALL 重置内部存储 + 调用 `rebuild_index` 从 MessageWriter 主存重新填充 + 发 EventMsg `sqlite_schema_rebuilt`（含 `old_version / new_version / rebuilt_thread_count / elapsed_ms`）。

#### Scenario: 老用户首次升级到带索引版本
- **WHEN** JSONL 主存已有 5 个 thread，但索引 db 文件不存在
- **THEN** 系统 SHALL 建空索引 + 从 JSONL 全量填充 5 条 metadata
- **AND** SHALL 发 `sqlite_schema_rebuilt`（old_version=0, new_version=当前代码版本, rebuilt_thread_count=5）

#### Scenario: schema 代码升级 v1 → v2
- **WHEN** 现有 db 内 `schema_meta.version=1`，代码升级到 v2 后启动
- **THEN** 系统 SHALL drop 所有表 + 重建 v2 表 + rebuild + 写入 v2 + 发 `sqlite_schema_rebuilt`

### Requirement: 默认 SQLite 实现损坏自愈

内置 SqliteThreadDirectory 启动时若检测到底层存储文件损坏，SHALL 把损坏文件重命名为 `<原名>.corrupt.<timestamp>` 保留 + 重建空索引 + rebuild + 发 EventMsg `sqlite_db_corrupt_rebuilt`（含原文件路径）。

#### Scenario: db 文件被截断
- **WHEN** db 文件被外部 truncate 到 0 字节
- **THEN** 系统 SHALL 检测损坏 + rename 备份 + 重建 + rebuild + 发事件

### Requirement: 默认实现不阻塞 event loop

内置 SqliteThreadDirectory 的任一 async 方法 SHALL 通过 thread pool 派发其底层同步 IO，不阻塞调用方 event loop。

#### Scenario: 主 loop 在 sqlite 调用期间仍可跑其它 task
- **WHEN** 一个 task 正在调用 SqliteThreadDirectory 写大批 metadata（人为模拟 1s 慢调用），另一 task 调 `anyio.sleep(0.01)` 然后记录耗时
- **THEN** 另一 task 的耗时 SHALL 接近 0.01s（远小于 1s），证明 event loop 未被阻塞

### Requirement: NullThreadDirectory 零副作用

`NullThreadDirectory` 任一方法被调用 SHALL 不写任何文件 / 不打开任何连接 / 不抛异常 / 不发事件；`list_threads()` SHALL 返回 `ThreadPage(items=[], next_cursor=None)`；`get_metadata()` SHALL 返回 None。

#### Scenario: 调用任意方法不触达文件系统
- **WHEN** monkeypatch `sqlite3.connect` 与 `open` 让它们 raise，构造 NullThreadDirectory 并调用所有方法
- **THEN** SHALL 不抛任何异常，方法返回值符合零结果约定

