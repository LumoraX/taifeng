"""JSONL 原子 batch frame、跨 writer 文件锁与同步重放实现。"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import ValidationError

from taifeng.conversation.models import ResponseItem

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def append_durable(path: Path, content: str) -> None:
    """单个 worker 内追加完整 frame，并在返回前 flush/fsync。"""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def append_buffered(path: Path, content: str) -> None:
    """保持 legacy append 的无 fsync 行为与延迟边界。"""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(content)


def _open_lock_file(path: Path) -> Any:
    """在线程 worker 中打开每个 thread 共用的跨 writer 锁文件。"""
    return path.open("a+b")


def _lock_exclusive(handle: Any) -> None:
    """使用当前平台的 advisory file lock 阻塞取得排他所有权。"""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_and_close(handle: Any) -> None:
    """释放 advisory lock 并关闭句柄；始终在 worker thread 执行。"""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@asynccontextmanager
async def exclusive_file_lock(path: Path) -> AsyncIterator[None]:
    """异步持有跨 writer 锁，所有阻塞 lock 系统调用均下沉 worker。"""
    handle = await anyio.to_thread.run_sync(_open_lock_file, path)
    try:
        await anyio.to_thread.run_sync(_lock_exclusive, handle)
        yield
    finally:
        await anyio.to_thread.run_sync(_unlock_and_close, handle)


def canonical_item_line(item: ResponseItem) -> str:
    """生成用于 wire 与 digest 的唯一 canonical JSON 行。"""
    return json.dumps(
        item.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def frame_line(
    frame: str,
    *,
    frame_id: str,
    batch_id: str,
    item_ids: tuple[str, ...],
    digest: str,
) -> str:
    """序列化原子 batch transport frame；它不是 conversation item。"""
    return json.dumps(
        {
            "frame": frame,
            "frame_id": frame_id,
            "batch_id": batch_id,
            "item_ids": list(item_ids),
            "digest": digest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class BatchDescriptor:
    """已规范化的待写 batch。"""

    items: tuple[ResponseItem, ...]
    item_lines: tuple[str, ...]
    item_ids: tuple[str, ...]
    digest: str


def describe_batch(items: list[ResponseItem], batch_id: str) -> BatchDescriptor:
    """注入 commit metadata 并计算可重放 digest。"""
    prepared = tuple(
        item.model_copy(
            update={"metadata": {**item.metadata, "commit_batch_id": batch_id}}
        )
        for item in items
    )
    lines = tuple(canonical_item_line(item) for item in prepared)
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return BatchDescriptor(
        items=prepared,
        item_lines=lines,
        item_ids=tuple(item.id for item in prepared),
        digest=digest,
    )


@dataclass(slots=True)
class _ActiveBatch:
    """reader 正在等待 commit 的物理 frame。"""

    frame_id: str
    batch_id: str
    item_ids: tuple[str, ...]
    digest: str
    items: list[ResponseItem]
    item_lines: list[str]


@dataclass(frozen=True, slots=True)
class ReadState:
    """一次 JSONL 同步读取的可见状态与待异步上报损坏行。"""

    items: tuple[ResponseItem, ...]
    committed: dict[str, tuple[str, tuple[str, ...]]]
    corrupt: tuple[tuple[int, Exception], ...]


def _active_batch_from_frame(data: dict[str, Any]) -> _ActiveBatch | None:
    """仅接受字段类型完整的 begin frame。"""
    frame_id = data.get("frame_id")
    batch_id = data.get("batch_id")
    digest = data.get("digest")
    raw_ids = data.get("item_ids")
    if not all(isinstance(value, str) and value for value in (frame_id, batch_id, digest)):
        return None
    if not isinstance(raw_ids, list) or not raw_ids or not all(
        isinstance(item_id, str) and item_id for item_id in raw_ids
    ):
        return None
    return _ActiveBatch(
        frame_id=frame_id,
        batch_id=batch_id,
        item_ids=tuple(raw_ids),
        digest=digest,
        items=[],
        item_lines=[],
    )


def _frame_commits(active: _ActiveBatch, data: dict[str, Any]) -> bool:
    """核对 commit frame、实际 item ids 与 canonical digest。"""
    actual_ids = tuple(item.id for item in active.items)
    actual_digest = hashlib.sha256(
        "\n".join(active.item_lines).encode("utf-8")
    ).hexdigest()
    return (
        data.get("frame_id") == active.frame_id
        and data.get("batch_id") == active.batch_id
        and tuple(data.get("item_ids") or ()) == active.item_ids
        and data.get("digest") == active.digest
        and actual_ids == active.item_ids
        and actual_digest == active.digest
    )


def read_state(path: Path) -> ReadState:
    """在线程 worker 中重放 bare items 与完整 committed frames。"""
    if not path.exists():
        return ReadState((), {}, ())
    result: list[ResponseItem] = []
    committed: dict[str, tuple[str, tuple[str, ...]]] = {}
    corrupt: list[tuple[int, Exception]] = []
    active: _ActiveBatch | None = None
    with path.open("r", encoding="utf-8") as stream:
        for line_no, raw in enumerate(stream, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                corrupt.append((line_no, exc))
                active = None
                continue
            if isinstance(data, dict) and data.get("__meta__"):
                continue
            frame = data.get("frame") if isinstance(data, dict) else None
            if frame == "item_batch_begin":
                active = _active_batch_from_frame(data)
                continue
            if frame == "item_batch_commit":
                if active is not None and _frame_commits(active, data):
                    identity = (active.digest, active.item_ids)
                    if active.batch_id not in committed:
                        result.extend(active.items)
                        committed[active.batch_id] = identity
                active = None
                continue
            try:
                item = ResponseItem.model_validate(data)
            except ValidationError as exc:
                corrupt.append((line_no, exc))
                active = None
                continue
            if active is None and "commit_batch_id" in item.metadata:
                continue
            if active is None:
                result.append(item)
            elif item.metadata.get("commit_batch_id") == active.batch_id:
                active.items.append(item)
                active.item_lines.append(canonical_item_line(item))
            else:
                active = None
    return ReadState(tuple(result), committed, tuple(corrupt))
