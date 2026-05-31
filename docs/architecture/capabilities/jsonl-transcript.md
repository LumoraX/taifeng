# jsonl-transcript Specification

## Purpose
TBD - created by archiving change store-protocol-decoupling. Update Purpose after archive.
## Requirements
### Requirement: MessageWriter 协议形状

系统 SHALL 提供 `MessageWriter` 作为 `typing.Protocol` 且 `runtime_checkable`，方法签名固定为：

- `async def create_thread(*, entry_skill_id: str, source: str = "user", tags: tuple[str, ...] = (), extra: dict | None = None) -> str`
- `async def append(thread_id: str, items: list[ResponseItem]) -> None`
- `async def load_history(thread_id: str) -> list[ResponseItem]`

#### Scenario: 业务自定义实现可被识别
- **WHEN** 业务侧自定义类实现以上 3 个 async 方法
- **THEN** `isinstance(instance, MessageWriter)` SHALL 返回 True

### Requirement: create_thread 生成稳定 ID 并初始化主存

`writer.create_thread(entry_skill_id=...)` SHALL 生成全局唯一 thread_id，SHALL 创建该 thread 的主存载体并在首条写入 ThreadMetadata（thread 自包含元数据，rebuild 时可恢复），SHALL 返回 thread_id。

#### Scenario: 默认实现首行是 metadata
- **WHEN** `tid = await writer.create_thread(entry_skill_id="general")`，读取 `<threads_dir>/<tid>.jsonl` 第一行
- **THEN** 第一行 SHALL 解析为 JSON 对象且包含 `__meta__: true` 标志
- **AND** 对象 SHALL 含 `thread_id == tid` / `entry_skill_id == "general"` / `created_at` / `updated_at` / `source` / `tags` / `extra`

#### Scenario: 多次调用 thread_id 全局唯一
- **WHEN** 连续 100 次 create_thread
- **THEN** 返回的 100 个 thread_id SHALL 全部不重复

### Requirement: append 是 append-only

`writer.append(thread_id, items)` SHALL 把 items 追加到主存末尾，SHALL NOT 修改或删除任何已写入的 item；并发 append（同一 thread 多个 task）SHALL NOT 撕裂单个 item 的序列化结果。

#### Scenario: 5 个 task 并发 append 同一 thread 无丢失
- **WHEN** 5 个 task 同时调 `writer.append(tid, [item_i])`（每个 item 序列化 < 4KB）
- **THEN** `await writer.load_history(tid)` 返回的列表 SHALL 含全部 5 个 item（顺序可任意）
- **AND** 无单条 item 被撕裂为不可解析

#### Scenario: 已写入的 item 不被修改
- **WHEN** 先 `append(tid, [item_A])`，再 `append(tid, [item_B])`，然后 `load_history(tid)`
- **THEN** 返回结果 SHALL 包含 item_A 与 item_B 且 item_A 内容与首次 append 时完全相同

### Requirement: load_history 完整回放跳过 metadata

`writer.load_history(thread_id)` SHALL 返回该 thread 全部 ResponseItem 按写入顺序，SHALL 跳过首条 metadata 行，SHALL 跳过损坏的单行 + 发 EventMsg `transcript_skipped_corrupt_line`（不抛异常）。

#### Scenario: load_history 不包含 metadata 首行
- **WHEN** create_thread 后 append 2 个 item，调 `load_history(tid)`
- **THEN** 返回结果 SHALL 恰好包含 2 个 item，不含 metadata 字典

#### Scenario: 中间一行损坏被跳过
- **WHEN** 主存文件中间被外部写入一行非 JSON 字符串后，调 `load_history(tid)`
- **THEN** 调用 SHALL NOT 抛异常
- **AND** 返回结果 SHALL 含其它有效 item
- **AND** 事件流 SHALL 含 EventMsg `transcript_skipped_corrupt_line`

### Requirement: JsonlMessageStore 旧 API 完全保留

引用 `taifeng.JsonlMessageStore` SHALL 得到 `JsonlMessageWriter + 默认 ThreadDirectory + NoopIndexHook` 的兼容封装；旧 API（`create_thread` / `append` / `load_history` / `list_threads` / `get_metadata`）SHALL 保持向后兼容；旧业务代码 SHALL 无需任何改动即可运行。

#### Scenario: 老代码无修改可跑
- **WHEN** 业务侧 `store = JsonlMessageStore(threads_dir=Path("./data"))`，依次调 `tid = await store.create_thread(entry_skill_id="x")` / `await store.append(tid, [item])` / `items = await store.load_history(tid)` / `page = await store.list_threads(limit=10)`
- **THEN** 所有调用 SHALL 与改造前行为等价
- **AND** page.items SHALL 含刚创建的 thread

#### Scenario: 默认 directory 自动指向 SQLite
- **WHEN** `store = JsonlMessageStore(threads_dir=Path("./data"))`
- **THEN** 调用 `store.list_threads(...)` 内部 SHALL 走 SqliteThreadDirectory（验证：`./data/taifeng-index.db` 文件存在）

### Requirement: rebuild_index 从主存全量重建

`rebuild_index(writer, directory, *, dry_run: bool = False) -> RebuildReport` SHALL 枚举 writer 主存中的所有 thread，对每个 thread 解析 ThreadMetadata；若 `dry_run=False` SHALL 调用 `directory.upsert_metadata(meta)` 批量填充；若 `dry_run=True` SHALL 仅扫描 + 计数；SHALL 返回 `RebuildReport(scanned_count, indexed_count, orphan_count, error_count, elapsed_ms)`。

#### Scenario: 删除 SQLite 后从 JSONL 全量重建
- **WHEN** 已有 5 个 thread 的 JSONL 主存，外部删除 `taifeng-index.db`，重新构造 SqliteThreadDirectory 并调 `rebuild_index(writer, directory)`
- **THEN** report.scanned_count SHALL == 5
- **AND** report.indexed_count SHALL == 5
- **AND** report.error_count SHALL == 0
- **AND** 重建后 `directory.list_threads(limit=10)` SHALL 含全部 5 个 thread

#### Scenario: dry_run 不修改 directory
- **WHEN** directory 当前为空，调 `rebuild_index(writer, directory, dry_run=True)`
- **THEN** report SHALL 反映正确的 scanned_count
- **AND** 调用后 `await directory.list_threads()` 的 items 长度 SHALL == 0（未被修改）

#### Scenario: 损坏首行计入 error_count 不中断
- **WHEN** 5 个 thread 中第 3 个的 JSONL 首行被人为破坏，调 `rebuild_index(...)`
- **THEN** report.error_count SHALL == 1
- **AND** report.indexed_count SHALL == 4
- **AND** 事件流 SHALL 含 EventMsg `rebuild_skipped_corrupt`（含损坏 thread 标识 + cause）

#### Scenario: rebuild 幂等
- **WHEN** 连续两次调用 `await rebuild_index(writer, directory)`
- **THEN** 第二次 report.indexed_count SHALL == 第一次
- **AND** 第二次执行后 `directory.list_threads(...)` 的内容 SHALL 与第一次完成后等价

### Requirement: EnginePool resume by thread_id

`EnginePool.get_or_create` SHALL 接收可选 kwarg `resume_thread_id: str | None = None`：

- 若 `resume_thread_id is None`，行为不变（调 `store.create_thread` 新开 thread）。
- 若 `resume_thread_id` 非空，pool SHALL 调 `store.load_thread(resume_thread_id)` 物化历史；用该 thread_id + 加载的 items 列表构造 `AgentEngine`，**不**新建 thread。
- 已加载 history 中的 items SHALL 通过 `AgentEngine.__init__(initial_history=...)` kwarg 注入到 `engine._history`。
- 若 `resume_thread_id` 在 store 中不存在或加载结果为空，pool SHALL raise `ValueError`，**不**静默回退到 create_thread。
- `_cache_anchor_index` SHALL 在 resume 后保持初始值 `-1`（跨进程不可信任 provider prompt cache）。

`AgentEngine.__init__` SHALL 接收可选 kwarg `initial_history: list[ResponseItem] | None = None`：

- `None` → `self._history = []`（既有行为）
- 非 None → `self._history = list(initial_history)`（拷贝，防止外部修改影响 engine 状态）
- engine 自身 SHALL NOT 调 `store.load_thread` —— 加载责任在 pool 层

构造完成后，pool SHALL 通过 `engine._emit` 投递一条 `thread_resumed` 事件，data 含：

- `thread_id: str` —— 被 resume 的 thread
- `item_count: int` —— 加载的 ResponseItem 数量
- `entry_skill_id_at_resume: str` —— 本次 get_or_create 调用传入的 entry_skill_id
- `entry_skill_id_recorded: str | None` —— thread 创建时记录的 entry_skill_id（来自 ThreadDirectory metadata；不可用时为 None）

#### Scenario: 成功 resume 已有 thread
- **WHEN** 业务侧第一次 `pool_a.get_or_create(session_id="s1", entry_skill_id="x")` 跑过 1 轮（产生 user_message + assistant_message + 持久化）
- **AND** `pool_a.close()`
- **AND** 业务侧第二个 pool `pool_b.get_or_create(session_id="s1", entry_skill_id="x", resume_thread_id=<thread_id>)`
- **THEN** 新 engine 的 `history_snapshot()` SHALL 包含原 2 条 item（user_message + assistant_message）
- **AND** `engine.thread_id` SHALL 等于 `resume_thread_id`
- **AND** `engine._cache_anchor_index` SHALL 等于 -1

#### Scenario: resume 未知 thread_id 抛错
- **WHEN** 业务侧 `pool.get_or_create(session_id="s2", entry_skill_id="x", resume_thread_id="ghost-tid")`
- **AND** `ghost-tid` 在 store 中不存在
- **THEN** SHALL raise `ValueError`，错误消息含 `ghost-tid` 字样
- **AND** SHALL NOT 静默回退到 `create_thread`

#### Scenario: resume emit thread_resumed 事件
- **WHEN** 业务侧 resume 一个含 N 个 item 的 thread
- **AND** 业务侧在 `get_or_create` 返回后调 `engine.subscribe_all()` 订阅
- **THEN** 后续 emit 流中 SHALL 出现 `thread_resumed` 事件
- **AND** `data.item_count` 等于 N
- **AND** `data.thread_id` 等于 resume 的 thread_id

#### Scenario: resume 与 session cache 命中互斥
- **WHEN** session_id "s3" 已有 cached engine（之前 get_or_create 创建过）
- **AND** 业务侧再次 `pool.get_or_create(session_id="s3", entry_skill_id="x", resume_thread_id="some-tid")`
- **THEN** SHALL 返回既有 cached engine
- **AND** SHALL NOT 触发 resume 流程（既有 engine 的 history 不变；不 emit `thread_resumed`）

#### Scenario: initial_history 是拷贝
- **WHEN** 业务侧手动构造 `AgentEngine(initial_history=my_list)`
- **AND** 后续 `my_list.append(...)` 在外部修改
- **THEN** `engine.history_snapshot()` SHALL NOT 受外部修改影响

