"""SqliteThreadDirectory —— ThreadDirectory 协议的默认实现，stdlib sqlite3 backed。

**红线 R1**：本文件是 ``src/`` 内**唯一**允许 ``import sqlite3`` 的位置（T9 grep 强制）。

设计要点：

- **derived index**：JSONL 主存是 source-of-truth；本 directory 是可重建的索引
- **schema 演化策略**：表里有 ``schema_meta(version)`` 行；启动期版本不匹配 →
  drop tables + 调内置 ``_rebuild_from_jsonl`` 从 ``<threads_dir>/*.jsonl`` 全量重建 →
  发 ``sqlite_schema_rebuilt`` 事件（避免引 alembic 等 migration 工具）
- **损坏自愈**：启动期 ``PRAGMA integrity_check`` 失败 → rename ``<原名>.corrupt.<ts>`` 备份 →
  重建 → 发 ``sqlite_db_corrupt_rebuilt`` 事件
- **不阻塞 event loop**：所有 sqlite3 同步调用通过 ``anyio.to_thread.run_sync`` 派发
- **并发安全**：单 connection（``check_same_thread=False``）+ 内部 ``anyio.Lock`` 串行化所有调用
- **WAL mode**：``PRAGMA journal_mode=WAL`` + ``synchronous=NORMAL``，单机多 worker 共享 db 无锁竞争
- **orphan 检测**：list_threads 即将返回的 thread_id 若 ``<threads_dir>/<thread_id>.jsonl`` 不存在
  → 跳过 + 发 ``thread_indexed_orphan``

参照：codex ``codex-rs/rollout/src/state_db.rs`` 的 WAL + schema 管理范式。
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.conversation.errors import ThreadNotFoundError
from taifeng.conversation.models import (
    ThreadFilter,
    ThreadMetadata,
    ThreadPage,
)
from taifeng.loop.event import (
    DirectoryCursorReset,
    EventMsg,
    SqliteDbCorruptRebuilt,
    SqliteSchemaRebuilt,
    ThreadIndexedOrphan,
)

if TYPE_CHECKING:
    # 通过 telemetry/__init__.py 会循环引用 (telemetry → loop → context → conversation)
    from taifeng.telemetry.sink import TelemetrySink


CURRENT_SCHEMA_VERSION = 1

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS thread (
    thread_id       TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    entry_skill_id  TEXT NOT NULL,
    source          TEXT NOT NULL,
    tags_json       TEXT NOT NULL,
    extra_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thread_updated_at ON thread(updated_at DESC, thread_id DESC);
CREATE INDEX IF NOT EXISTS idx_thread_entry_skill ON thread(entry_skill_id);
"""


class SqliteThreadDirectory:
    """ThreadDirectory 协议的 stdlib SQLite 实现。

    构造时不连接 db；首次 async 方法调用触发 lazy init（``_ensure_init``）。
    init 包含：建表 / WAL / schema_meta 版本比对 / integrity_check / 必要时 drop+rebuild。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        threads_dir: str | Path,
        schema_version: int = CURRENT_SCHEMA_VERSION,
        sink: "TelemetrySink | None" = None,
    ) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._threads_dir = Path(threads_dir).expanduser().resolve()
        self._schema_version = schema_version
        self._sink = sink
        self._conn: sqlite3.Connection | None = None
        self._lock = anyio.Lock()
        self._initialized = False

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def _ensure_init(self) -> None:
        """惰性初始化：首次方法调用触发；后续幂等返回。"""
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await anyio.to_thread.run_sync(self._sync_init)
            # init 后判断是否需要 rebuild / 发事件（这些路径是 async）
            await self._post_init_check()
            self._initialized = True

    def _sync_init(self) -> None:
        """同步初始化：连接 db + 开启 WAL + 建表。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False 允许跨 thread 调用；外层 anyio.Lock 保证串行
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        self._conn.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        self._conn.executescript(_SCHEMA_DDL)

    async def _post_init_check(self) -> None:
        """init 完成后执行：integrity_check / schema_version 比对，必要时 rebuild + 发事件。"""
        # 1. 检测 db 损坏
        try:
            integrity_ok = await anyio.to_thread.run_sync(self._sync_integrity_check)
        except sqlite3.DatabaseError:
            integrity_ok = False
        if not integrity_ok:
            await self._handle_corrupt_db()
            return  # 损坏自愈后退出（_handle_corrupt_db 内部会重 init + rebuild）

        # 2. 检测 schema 版本
        current_version = await anyio.to_thread.run_sync(self._sync_get_schema_version)
        if current_version != self._schema_version:
            await self._handle_schema_mismatch(old_version=current_version, new_version=self._schema_version)

    def _sync_integrity_check(self) -> bool:
        assert self._conn is not None
        cur = self._conn.execute("PRAGMA integrity_check;")
        row = cur.fetchone()
        return bool(row and row[0] == "ok")

    def _sync_get_schema_version(self) -> int:
        assert self._conn is not None
        cur = self._conn.execute("SELECT version FROM schema_meta LIMIT 1;")
        row = cur.fetchone()
        return int(row[0]) if row else 0  # 0 = 全新 db / 未写入版本

    async def _handle_corrupt_db(self) -> None:
        """db 损坏：关闭连接 → rename 备份 → 重新 init → rebuild → 发事件。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        ts = int(time.time())
        backup_path = self._db_path.with_suffix(f".db.corrupt.{ts}")
        self._db_path.rename(backup_path)
        await anyio.to_thread.run_sync(self._sync_init)
        rebuilt_count = await self._rebuild_from_jsonl()
        await anyio.to_thread.run_sync(self._sync_write_schema_version, self._schema_version)
        await self._emit(
            SqliteDbCorruptRebuilt(
                data={"backup_path": str(backup_path), "rebuilt_thread_count": rebuilt_count}
            )
        )

    async def _handle_schema_mismatch(self, *, old_version: int, new_version: int) -> None:
        """schema 不匹配：drop 所有表 → 重建 → rebuild from JSONL → 写新版本 → 发事件。"""
        started = time.perf_counter()
        await anyio.to_thread.run_sync(self._sync_drop_and_recreate_tables)
        rebuilt_count = await self._rebuild_from_jsonl()
        await anyio.to_thread.run_sync(self._sync_write_schema_version, new_version)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        await self._emit(
            SqliteSchemaRebuilt(
                data={
                    "old_version": old_version,
                    "new_version": new_version,
                    "rebuilt_thread_count": rebuilt_count,
                    "elapsed_ms": elapsed_ms,
                }
            )
        )

    def _sync_drop_and_recreate_tables(self) -> None:
        assert self._conn is not None
        self._conn.executescript("DROP TABLE IF EXISTS schema_meta; DROP TABLE IF EXISTS thread;")
        self._conn.executescript(_SCHEMA_DDL)

    def _sync_write_schema_version(self, version: int) -> None:
        assert self._conn is not None
        self._conn.execute("DELETE FROM schema_meta;")
        self._conn.execute("INSERT INTO schema_meta(version) VALUES (?);", (version,))

    async def _rebuild_from_jsonl(self) -> int:
        """从 ``<threads_dir>/*.jsonl`` 首行 metadata 重建 thread 表。

        T6 会把这段逻辑提升为公共 ``rebuild_index`` 函数；目前内置以解耦 T3/T6 顺序。
        损坏的首行直接跳过，不计入 indexed_count（也不发事件，留给 T6 做）。
        """
        count = 0
        if not self._threads_dir.exists():
            return 0
        for path in sorted(self._threads_dir.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue
                data = json.loads(first_line)
                if not (isinstance(data, dict) and data.get("__meta__")):
                    continue
                meta = ThreadMetadata(
                    thread_id=data["thread_id"],
                    created_at=float(data["created_at"]),
                    updated_at=float(data["updated_at"]),
                    entry_skill_id=data["entry_skill_id"],
                    source=data.get("source", "user"),
                    tags=tuple(data.get("tags", [])),
                    extra=dict(data.get("extra", {})),
                )
                await anyio.to_thread.run_sync(self._sync_upsert, meta)
                count += 1
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                # rebuild 阶段坏行静默跳过；T6 公共 rebuild_index 会发 rebuild_skipped_corrupt
                continue
        return count

    async def close(self) -> None:
        """关闭 db 连接；可多次调用幂等。"""
        async with self._lock:
            if self._conn is not None:
                await anyio.to_thread.run_sync(self._conn.close)
                self._conn = None
            self._initialized = False

    # -----------------------------------------------------------------
    # ThreadDirectory protocol methods
    # -----------------------------------------------------------------

    async def list_threads(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        filter: ThreadFilter | None = None,
    ) -> ThreadPage:
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit must be in [1, 1000]; got {limit}")
        await self._ensure_init()

        # cursor 解码（损坏 → 从头开始 + 发事件）
        cursor_updated_at: float | None = None
        cursor_thread_id: str | None = None
        if cursor is not None:
            try:
                payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
                cursor_updated_at = float(payload["updated_at"])
                cursor_thread_id = str(payload["thread_id"])
            except (ValueError, KeyError, json.JSONDecodeError, binascii.Error) as e:
                await self._emit(DirectoryCursorReset(data={"cursor": cursor, "cause": repr(e)}))
                cursor_updated_at = None
                cursor_thread_id = None

        # 一次多取一些用于 orphan 过滤后补足 limit；这里固定 ×2 估算
        async with self._lock:
            rows = await anyio.to_thread.run_sync(
                self._sync_select_threads,
                limit,
                cursor_updated_at,
                cursor_thread_id,
                filter,
            )

        # orphan 过滤
        items: list[ThreadMetadata] = []
        for row in rows:
            meta = _row_to_metadata(row)
            jsonl_path = self._threads_dir / f"{meta.thread_id}.jsonl"
            if not jsonl_path.exists():
                await self._emit(ThreadIndexedOrphan(data={"thread_id": meta.thread_id}))
                continue
            items.append(meta)
            if len(items) >= limit:
                break

        # 计算 next_cursor：取最后一项的 (updated_at, thread_id)
        next_cursor: str | None = None
        if len(items) == limit and len(rows) >= limit:
            last = items[-1]
            payload = {"updated_at": last.updated_at, "thread_id": last.thread_id}
            next_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        return ThreadPage(items=items, next_cursor=next_cursor)

    async def get_metadata(self, thread_id: str) -> ThreadMetadata | None:
        await self._ensure_init()
        async with self._lock:
            row = await anyio.to_thread.run_sync(self._sync_select_one, thread_id)
        return _row_to_metadata(row) if row is not None else None

    async def update_metadata(self, thread_id: str, patch: dict[str, Any]) -> None:
        await self._ensure_init()
        async with self._lock:
            row = await anyio.to_thread.run_sync(self._sync_select_one, thread_id)
            if row is None:
                raise ThreadNotFoundError(thread_id)
            old = _row_to_metadata(row)
            merged = ThreadMetadata(
                thread_id=old.thread_id,
                created_at=old.created_at,
                updated_at=time.time(),
                entry_skill_id=patch.get("entry_skill_id", old.entry_skill_id),
                source=patch.get("source", old.source),
                tags=tuple(patch.get("tags", old.tags)),
                extra=dict(patch.get("extra", old.extra)),
            )
            await anyio.to_thread.run_sync(self._sync_upsert, merged)

    async def upsert_metadata(self, meta: ThreadMetadata) -> None:
        await self._ensure_init()
        async with self._lock:
            await anyio.to_thread.run_sync(self._sync_upsert, meta)

    # -----------------------------------------------------------------
    # 同步 DB 操作（在 thread pool 内执行）
    # -----------------------------------------------------------------

    def _sync_upsert(self, meta: ThreadMetadata) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT OR REPLACE INTO thread "
            "(thread_id, created_at, updated_at, entry_skill_id, source, tags_json, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                meta.thread_id,
                meta.created_at,
                meta.updated_at,
                meta.entry_skill_id,
                meta.source,
                json.dumps(list(meta.tags), ensure_ascii=False, separators=(",", ":")),
                json.dumps(meta.extra, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def _sync_select_one(self, thread_id: str) -> tuple[Any, ...] | None:
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT thread_id, created_at, updated_at, entry_skill_id, source, tags_json, extra_json "
            "FROM thread WHERE thread_id = ?",
            (thread_id,),
        )
        row = cur.fetchone()
        return tuple(row) if row is not None else None

    def _sync_select_threads(
        self,
        limit: int,
        cursor_updated_at: float | None,
        cursor_thread_id: str | None,
        filter: ThreadFilter | None,
    ) -> list[tuple[Any, ...]]:
        assert self._conn is not None
        # WHERE 子句拼装：filter + cursor 比较
        where_parts: list[str] = []
        params: list[Any] = []
        if filter is not None:
            if filter.entry_skill_id is not None:
                where_parts.append("entry_skill_id = ?")
                params.append(filter.entry_skill_id)
            if filter.created_after is not None:
                where_parts.append("created_at > ?")
                params.append(filter.created_after)
            if filter.created_before is not None:
                where_parts.append("created_at < ?")
                params.append(filter.created_before)
            if filter.source is not None:
                where_parts.append("source = ?")
                params.append(filter.source)
            if filter.tag is not None:
                where_parts.append("EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value = ?)")
                params.append(filter.tag)
        if cursor_updated_at is not None and cursor_thread_id is not None:
            # 复合游标：updated_at 严格小 OR 相等但 thread_id 严格小
            where_parts.append("(updated_at < ? OR (updated_at = ? AND thread_id < ?))")
            params.extend([cursor_updated_at, cursor_updated_at, cursor_thread_id])

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        # 多取一些（×2）以预留 orphan 过滤空间；调用方限到 limit
        fetch_limit = limit * 2
        sql = (
            "SELECT thread_id, created_at, updated_at, entry_skill_id, source, tags_json, extra_json "
            f"FROM thread {where_sql} ORDER BY updated_at DESC, thread_id DESC LIMIT ?"
        )
        cur = self._conn.execute(sql, (*params, fetch_limit))
        return cur.fetchall()

    # -----------------------------------------------------------------
    # 事件发射
    # -----------------------------------------------------------------

    async def _emit(self, msg: Any) -> None:
        """发系统级事件（sink 未注入则静默）。submission_id="*" 标记非 turn 上下文事件。"""
        if self._sink is None:
            return
        try:
            await self._sink.handle(EventMsg(submission_id="*", msg=msg))
        except Exception:
            # 事件发射失败不影响主路径（与 IndexHook fire-and-forget 同语义，但更严格：
            # directory 是主路径，事件失败必须吞）
            pass


def _row_to_metadata(row: tuple[Any, ...]) -> ThreadMetadata:
    """SQL row → ThreadMetadata（统一反序列化入口）。"""
    return ThreadMetadata(
        thread_id=row[0],
        created_at=float(row[1]),
        updated_at=float(row[2]),
        entry_skill_id=row[3],
        source=row[4],
        tags=tuple(json.loads(row[5])),
        extra=dict(json.loads(row[6])),
    )


__all__ = ["CURRENT_SCHEMA_VERSION", "SqliteThreadDirectory"]
