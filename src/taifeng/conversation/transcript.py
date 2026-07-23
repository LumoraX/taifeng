"""JSONL 主存实现。

包含 JsonlMessageWriter（新 MessageWriter 协议）和 JsonlMessageStore（legacy 兼容封装）。

文件布局：

- 新（``JsonlMessageWriter``）: ``<threads_dir>/<thread_id>.jsonl``（flat 布局）
  首行 ``{"__meta__": true, ...ThreadMetadata 字段}``（自包含，可用于 rebuild_index）
- 旧（``JsonlMessageStore``）:
  ``<threads_dir>/YYYY/MM/DD/rollout-{ts}-{thread_id}.jsonl``（date-bucketed）
  + ``<threads_dir>/index.db``（SQLite 旁路索引）

写入策略：

- 每条 ResponseItem 序列化为单行 JSON
- 用 ``O_APPEND`` + 单次 ``write()`` 保证 PIPE_BUF 内原子（单行通常 < 4KB）
- 不加跨进程锁；多进程 append 依赖 POSIX 4KB 写原子性

参照：

- codex codex-rs/rollout/src/{recorder,state_db}.rs
- openclaw src/config/sessions/transcript.ts
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import ValidationError

from taifeng.conversation.journal.materialization import (
    ProjectionFileIdentity,
    ProjectionSnapshot,
    ProjectionTargetHandle,
    safe_thread_path,
)
from taifeng.conversation.models import (
    ResponseItem,
    ThreadInfo,
    ThreadMetadata,
)
from taifeng.conversation.protocols import NoopIndexHook
from taifeng.conversation.sqlite_directory import SqliteThreadDirectory
from taifeng.conversation.store import MessageStore
from taifeng.loop.event import EventMsg, TranscriptSkippedCorruptLine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    # TelemetrySink 走 telemetry/__init__.py 会引入循环 (telemetry → loop → context → conversation)
    # 这里只用作类型注解，运行时不需要解析
    from taifeng.conversation.journal.projector import ProjectionResult
    from taifeng.telemetry.sink import TelemetrySink


def _generate_thread_id() -> str:
    return f"thr_{secrets.token_hex(8)}"


def _iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _exclusive_write(path: Path, line: str) -> None:
    """在线程 worker 中 exclusive-create 一个 metadata 文件。"""
    with path.open("x", encoding="utf-8") as stream:
        stream.write(line)


def _is_audited_metadata(extra: dict[str, Any]) -> bool:
    """验证派生目录保留了完整 audited projection marker。"""
    session_id = extra.get("journal_session_id")
    schema_version = extra.get("journal_schema_version")
    return (
        extra.get("audit_required") is True
        and isinstance(session_id, str)
        and bool(session_id)
        and not isinstance(schema_version, bool)
        and isinstance(schema_version, int)
        and schema_version >= 1
    )


def _metadata_record(metadata: ThreadMetadata) -> dict[str, object]:
    """把 directory metadata 还原为 JSONL 自包含首行。"""
    return {
        "__meta__": True,
        "thread_id": metadata.thread_id,
        "created_at": metadata.created_at,
        "updated_at": metadata.updated_at,
        "entry_skill_id": metadata.entry_skill_id,
        "source": metadata.source,
        "tags": list(metadata.tags),
        "extra": dict(metadata.extra),
    }


# -----------------------------------------------------------------
# JsonlMessageWriter —— MessageWriter 协议的默认实现（store-protocol-decoupling T2）
# -----------------------------------------------------------------


def iter_thread_files(threads_dir: Path) -> Iterator[Path]:
    """枚举 flat 布局下所有 thread JSONL 文件。

    供 ``rebuild_index`` (T6) 与运维脚本使用；按文件名字典序返回稳定顺序。
    """
    root = Path(threads_dir).expanduser().resolve()
    if not root.exists():
        return
    for path in sorted(root.glob("*.jsonl")):
        if path.is_file():
            yield path


class JsonlMessageWriter:
    """JSONL 主存 —— ``MessageWriter`` 协议的默认实现。

    布局：``<threads_dir>/<thread_id>.jsonl``。每个 thread 一个文件，首行写 ``__meta__``
    元数据（自包含，可被 ``rebuild_index`` 还原 ``ThreadDirectory``）。

    并发模型：

    - 不加跨进程锁
    - 每次 ``append`` 用 ``open(path, 'a')`` + 单次 ``write()``
    - POSIX 在 PIPE_BUF（4KB）内保证不撕裂行
    - 多 task / 多进程并发 append 同 thread：序列化结果完整，但顺序不保证全局严格

    可选 ``sink``：传入后，``load_history`` 跳过损坏行时会发 ``transcript_skipped_corrupt_line``
    事件；未传则静默跳过。
    """

    def __init__(self, threads_dir: str | Path, *, sink: TelemetrySink | None = None) -> None:
        self._root = Path(threads_dir).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._sink = sink

    @property
    def threads_dir(self) -> Path:
        """主存根目录 —— 供 ``rebuild_index`` 等外部工具访问。"""
        return self._root

    def _thread_path(self, thread_id: str) -> Path:
        return safe_thread_path(self._root, thread_id)

    async def create_thread(
        self,
        *,
        entry_skill_id: str,
        source: str = "user",
        tags: tuple[str, ...] = (),
        extra: dict[str, Any] | None = None,
    ) -> str:
        """创建新 thread，写入首行 metadata，返回全局唯一 thread_id。"""
        thread_id = _generate_thread_id()
        return await self._create_thread_with_id(
            thread_id=thread_id,
            entry_skill_id=entry_skill_id,
            source=source,
            tags=tags,
            extra=extra,
        )

    async def _create_thread_with_id(
        self,
        *,
        thread_id: str,
        entry_skill_id: str,
        source: str,
        tags: tuple[str, ...] = (),
        extra: dict[str, Any] | None = None,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> str:
        """以调用方预分配 id exclusive-create thread；仅供内核投影 bootstrap。"""
        now = time.time() if created_at is None else created_at
        updated = now if updated_at is None else updated_at
        meta_line = {
            "__meta__": True,
            "thread_id": thread_id,
            "created_at": now,
            "updated_at": updated,
            "entry_skill_id": entry_skill_id,
            "source": source,
            "tags": list(tags),
            "extra": dict(extra) if extra else {},
        }
        path = self._thread_path(thread_id)
        line = json.dumps(meta_line, ensure_ascii=False, separators=(",", ":")) + "\n"
        await anyio.to_thread.run_sync(_exclusive_write, path, line)
        return thread_id

    async def append(self, thread_id: str, items: list[ResponseItem]) -> None:
        """追加一组 item 到 thread 主存末尾。append-only，不修改已写入内容。"""
        if not items:
            return
        path = self._thread_path(thread_id)
        with path.open("a", encoding="utf-8") as f:
            for item in items:
                line = json.dumps(
                    item.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                # 单次 write 保证 < PIPE_BUF 行原子（line 应 < 4KB）
                f.write(line + "\n")

    async def load_history(self, thread_id: str) -> list[ResponseItem]:
        """按写入顺序回放 thread 全部 item。

        - 跳过首条 ``__meta__`` 行
        - 跳过损坏行（json decode 失败 / pydantic 校验失败）
        - 损坏行 SHALL 发 EventMsg ``transcript_skipped_corrupt_line``（若 ``sink`` 已注入）
        - 任何情况下 SHALL NOT 抛异常
        """
        path = self._thread_path(thread_id)
        if not path.exists():
            return []
        result: list[ResponseItem] = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                # 损坏行处理：捕获 JSON 解码失败 + pydantic 校验失败
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as e:
                    await self._emit_corrupt(thread_id, line_no, e)
                    continue
                # 跳过 metadata 首行（首行规约：data 是 dict 且含 __meta__）
                if isinstance(data, dict) and data.get("__meta__"):
                    continue
                try:
                    result.append(ResponseItem.model_validate(data))
                except ValidationError as e:
                    await self._emit_corrupt(thread_id, line_no, e)
        return result

    async def _emit_corrupt(self, thread_id: str, line_no: int, cause: Exception) -> None:
        """跳过损坏行时发事件（sink 未注入则静默）。

        submission_id="*" 标记系统级事件，因为 load_history 可能在 turn 外被调用
        （rebuild_index / 运维脚本）。
        """
        if self._sink is None:
            return
        ev = EventMsg(
            submission_id="*",
            msg=TranscriptSkippedCorruptLine(
                data={"thread_id": thread_id, "line_no": line_no, "cause": repr(cause)}
            ),
        )
        await self._sink.handle(ev)


# -----------------------------------------------------------------
# JsonlMessageStore —— 三协议组合的兼容封装（store-protocol-decoupling T4）
# -----------------------------------------------------------------


class JsonlMessageStore(MessageStore):
    """[Legacy] 兼容封装 = ``JsonlMessageWriter`` + ``SqliteThreadDirectory`` + ``NoopIndexHook``。

    旧业务代码 ``store = JsonlMessageStore(threads_dir)`` 无需任何改动即可运行。
    内部委派：

    - 消息体写读 → JsonlMessageWriter（``<threads_dir>/<thread_id>.jsonl``，新 flat 布局）
    - thread 元数据 → SqliteThreadDirectory（``<threads_dir>/taifeng-index.db``）
    - 业务事件 → NoopIndexHook（兼容封装内不暴露 hook 接口；要订阅请走新 API）

    遗留参数适配：

    - ``cwd`` 落到 ``ThreadMetadata.extra["cwd"]``（新协议无 cwd 一等公民）
    - ``entry_skill_id`` 旧版可 None；新协议要求非空 → 兜底 ``"general"``
    - ``source`` 旧版可 None → 兜底 ``"user"``
    - ``list_threads(cwd=...)`` 在 Python 侧按 ``extra.cwd`` 过滤（适合中小规模）
    - ``select_resume_path(cwd)`` 简化为「最近 cwd 匹配的 thread」（旧版 last_kind 启发不再追踪）
    - ``item_count`` 不再追踪（``ThreadInfo`` 返回 0），需要的话用 ``load_thread`` 计数

    **新代码不应使用本类**；改用
    ``AgentEngine(storage_dir=..., thread_directory=..., index_hook=...)``
    三协议接口（spec：docs/architecture/conversation.md
    """

    def __init__(self, threads_dir: str | Path) -> None:
        self._root = Path(threads_dir).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        # 三组件组合：主存 / 索引 / 事件钩子
        self._writer = JsonlMessageWriter(self._root)
        self._directory = SqliteThreadDirectory(
            self._root / "taifeng-index.db",
            threads_dir=self._root,
        )
        self._hook = NoopIndexHook()
        self._projection_target = ProjectionTargetHandle(self._root)

    # -----------------------------------------------------------------
    # Thread lifecycle
    # -----------------------------------------------------------------

    async def create_thread(
        self,
        *,
        cwd: str | None = None,
        entry_skill_id: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        # legacy None 兜底；spec ThreadMetadata 要求 entry_skill_id 非空
        eid = entry_skill_id or "general"
        src = source or "user"
        # cwd 兼容字段 + 调用方 extra（K7：parent_thread_id / spawn_depth 等）合并
        merged_extra: dict[str, Any] = {"cwd": cwd} if cwd is not None else {}
        if extra:
            merged_extra.update(extra)
        extra = merged_extra
        tid = await self._writer.create_thread(entry_skill_id=eid, source=src, extra=extra)
        # 同步注册到 directory（兼容封装内自动 upsert，避免业务再调一次）
        now = time.time()
        meta = ThreadMetadata(
            thread_id=tid,
            created_at=now,
            updated_at=now,
            entry_skill_id=eid,
            source=src,
            tags=(),
            extra=extra,
        )
        await self._directory.upsert_metadata(meta)
        return tid

    def projection_scope(self, thread_id: str) -> Any:
        """返回 handle admission 与物理 target thread lock 的组合 scope。"""
        return self._projection_target.scope(thread_id)

    async def load_projection_snapshot(self, thread_id: str) -> ProjectionSnapshot:
        """读取并验证 audited metadata，返回共享 cache snapshot。"""
        meta = await self._directory.get_metadata(thread_id)
        if meta is None or not _is_audited_metadata(meta.extra):
            raise FileNotFoundError(f"audited projection metadata not found: {thread_id}")
        return await self._projection_target.load_snapshot(
            thread_id,
            _metadata_record(meta),
        )

    async def append_projection_batch(
        self,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """通过 open-existing + file identity 边界追加 audited projection。"""
        await self._projection_target.append_batch(thread_id, items, expected_identity)

    def projection_scan_count(self, thread_id: str) -> int:
        """返回共享物理 target 的完整 snapshot 扫描次数。"""
        return self._projection_target.scan_count(thread_id)

    def projection_state(
        self, thread_id: str
    ) -> tuple[ProjectionResult | None, int | None]:
        """读取 materialization target 的共享 result 与 blocked seq。"""
        return self._projection_target.state(thread_id)

    def update_projection_state(
        self,
        thread_id: str,
        result: ProjectionResult,
        blocked_seq: int | None,
    ) -> None:
        """在 projection_lock 内更新共享 materialization state。"""
        self._projection_target.update_state(thread_id, result, blocked_seq)

    def projection_first_seq(self, thread_id: str) -> int | None:
        """返回物理 target 最早健康观察的 conversation seq。"""
        return self._projection_target.first_sequence(thread_id)

    def record_projection_first_seq(self, thread_id: str, seq: int) -> None:
        """首次健康投影后记录 generation replay 起点。"""
        self._projection_target.record_first_sequence(thread_id, seq)

    async def create_projection_thread(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        entry_skill_id: str,
        source: str,
        extra: dict[str, Any],
    ) -> str:
        """用预分配 id 创建默认 JSONL 投影，并同步登记 matching metadata。

        directory 是可重建派生索引；注册失败会向上传播并保留已 exclusive-create 的自包含
        JSONL，后续同 id 重试仍拒绝覆盖，运维可通过 rebuild_index 恢复索引。
        """
        merged_extra: dict[str, Any] = {"cwd": cwd} if cwd is not None else {}
        merged_extra.update(extra)
        now = time.time()
        tid = await self._writer._create_thread_with_id(  # noqa: SLF001
            thread_id=thread_id,
            entry_skill_id=entry_skill_id,
            source=source,
            extra=merged_extra,
            created_at=now,
        )
        meta = ThreadMetadata(
            thread_id=tid,
            created_at=now,
            updated_at=now,
            entry_skill_id=entry_skill_id,
            source=source,
            tags=(),
            extra=merged_extra,
        )
        await self._directory.upsert_metadata(meta)
        self._projection_target.invalidate(thread_id)
        return tid

    # -----------------------------------------------------------------
    # Append
    # -----------------------------------------------------------------

    async def append(self, item: ResponseItem) -> None:
        await self._writer.append(item.thread_id, [item])

    async def append_batch(self, items: list[ResponseItem]) -> None:
        if not items:
            return
        # 按 thread 分组转给 writer.append（writer 内部不再加锁，POSIX 4KB 原子保证）
        by_thread: dict[str, list[ResponseItem]] = {}
        for it in items:
            by_thread.setdefault(it.thread_id, []).append(it)
        for tid, group in by_thread.items():
            await self._writer.append(tid, group)

    # -----------------------------------------------------------------
    # Load
    # -----------------------------------------------------------------

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """Legacy: 返回 AsyncIterator 以保持旧 API 形态。

        新 API ``writer.load_history`` 直接返回 list，更简单；本兼容方法把 list 再包装。
        """
        items = await self._writer.load_history(thread_id)

        async def _gen() -> AsyncIterator[ResponseItem]:
            for it in items:
                yield it

        return _gen()

    # -----------------------------------------------------------------
    # List / resume
    # -----------------------------------------------------------------

    async def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int = 50,
    ) -> list[ThreadInfo]:
        # cwd 过滤不在新 ThreadFilter 字段里，先取更多再 Python 侧过滤
        fetch_limit = min(max(limit * 4, limit), 1000) if cwd is not None else limit
        page = await self._directory.list_threads(limit=fetch_limit)
        result: list[ThreadInfo] = []
        for meta in page.items:
            if cwd is not None and meta.extra.get("cwd") != cwd:
                continue
            result.append(
                ThreadInfo(
                    thread_id=meta.thread_id,
                    cwd=meta.extra.get("cwd"),
                    created_at=datetime.fromtimestamp(meta.created_at, tz=UTC),
                    last_activity_at=datetime.fromtimestamp(meta.updated_at, tz=UTC),
                    item_count=0,  # 兼容封装不再追踪每 thread 条数
                    entry_skill_id=meta.entry_skill_id,
                    source=meta.source,
                )
            )
            if len(result) >= limit:
                break
        return result

    async def select_resume_path(self, cwd: str) -> str | None:
        """Legacy: 旧版按 last_kind 启发；本兼容版简化为「最近 cwd 匹配的 thread」。

        新代码应在业务侧自行维护 resume 策略（taifeng 不再内置）。
        """
        threads = await self.list_threads(cwd=cwd, limit=1)
        return threads[0].thread_id if threads else None

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def close(self) -> None:
        await self._projection_target.close()
        await self._directory.close()
