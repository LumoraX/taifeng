"""Conversation projection 的物理目标共享资源与 handle 生命周期。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import sniffio
from pydantic import ValidationError

from taifeng.conversation.models import ResponseItem

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from taifeng.conversation.journal.projector import ProjectionResult


class ProjectionLifecycleError(RuntimeError):
    """投影 handle 已关闭或物理目标绑定到其他 async backend。"""


@dataclass(frozen=True, slots=True)
class ProjectionFileIdentity:
    """用于 audited append compare-before-write 的物理文件身份。"""

    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    """已验证 metadata 的 conversation item 快照与对应文件身份。"""

    items: tuple[ResponseItem, ...]
    identity: ProjectionFileIdentity
    history_reset: bool = False


@dataclass(frozen=True, slots=True)
class AuditedProjectionMarker:
    """legacy resume 门禁所需的最小 audited transcript identity。"""

    session_id: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class ProjectionReplayWindow:
    """generation reset 后恢复旧 healthy snapshot 的共享窗口。"""

    floor: int
    ceiling: int
    expected_items: tuple[ResponseItem, ...]
    progress: int | None = None


class _HandleState(StrEnum):
    """单个 store handle 的生命周期。"""

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class _PhysicalProjectionTarget:
    """按 resolved threads directory 唯一的同进程 materialization 资源。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.references = 0
        self._guard = threading.Lock()
        self.backend_token: object | None = None
        self.thread_locks: dict[str, anyio.Lock] = {}
        self.states: dict[str, tuple[ProjectionResult, int | None]] = {}
        self.first_sequences: dict[str, int] = {}
        self.session_ids: dict[str, str] = {}
        self.replay_windows: dict[str, ProjectionReplayWindow] = {}
        self.snapshots: dict[str, ProjectionSnapshot] = {}
        self.scan_counts: dict[str, int] = {}

    def bind_backend(self) -> None:
        """首次使用时绑定 event loop/backend，禁止活跃期跨 loop 复用。"""
        backend = sniffio.current_async_library()
        token: object = (
            asyncio.get_running_loop()
            if backend == "asyncio"
            else anyio.lowlevel.current_token()
        )
        with self._guard:
            if self.backend_token is None:
                self.backend_token = token
            elif self.backend_token is not token:
                raise ProjectionLifecycleError(
                    "projection target is active on a different event loop/backend"
                )

    def lock_for(self, thread_id: str) -> anyio.Lock:
        """返回物理 target 共享的 per-thread 锁。"""
        with self._guard:
            return self.thread_locks.setdefault(thread_id, anyio.Lock())

    def session_id_for(self, thread_id: str) -> str | None:
        """返回已绑定的 Journal Session identity。"""
        with self._guard:
            return self.session_ids.get(thread_id)

    def reserve_session_id(self, thread_id: str, session_id: str) -> bool:
        """原子预留 identity；返回是否由本次调用新建。"""
        with self._guard:
            existing = self.session_ids.get(thread_id)
            if existing is not None and existing != session_id:
                raise ProjectionLifecycleError(
                    "projection target is bound to another Journal Session"
                )
            if existing is not None:
                return False
            self.session_ids[thread_id] = session_id
            return True

    def release_session_id(self, thread_id: str, session_id: str) -> None:
        """仅撤销仍属于本次 identity 的未持久 reservation。"""
        with self._guard:
            if self.session_ids.get(thread_id) == session_id:
                self.session_ids.pop(thread_id, None)

    def clear(self) -> None:
        """最终 handle 释放时清除所有 backend-bound 派生状态。"""
        self.backend_token = None
        self.thread_locks.clear()
        self.states.clear()
        self.first_sequences.clear()
        self.session_ids.clear()
        self.replay_windows.clear()
        self.snapshots.clear()
        self.scan_counts.clear()


_REGISTRY_LOCK = threading.Lock()
_TARGETS: dict[Path, _PhysicalProjectionTarget] = {}


def safe_thread_path(root: Path, thread_id: str) -> Path:
    """校验 flat thread id，并证明候选路径的 resolved parent 等于 root。"""
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id in {".", ".."}
        or "\x00" in thread_id
        or "/" in thread_id
        or "\\" in thread_id
        or Path(thread_id).is_absolute()
    ):
        raise ValueError("unsafe thread_id")
    resolved_root = root.expanduser().resolve()
    path = resolved_root / f"{thread_id}.jsonl"
    if path.parent.resolve() != resolved_root:
        raise ValueError("unsafe thread_id parent")
    return path


def audited_projection_marker_from_extra(
    extra: object,
) -> AuditedProjectionMarker | None:
    """只解析完整 audit marker；声明 audit 却不完整时 fail-closed。"""
    if not isinstance(extra, dict) or extra.get("audit_required") is not True:
        return None
    session_id = extra.get("journal_session_id")
    schema_version = extra.get("journal_schema_version")
    if (
        not isinstance(session_id, str)
        or not session_id
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise ProjectionLifecycleError("incomplete audited projection marker")
    return AuditedProjectionMarker(session_id, schema_version)


def _identity(stat_result: os.stat_result) -> ProjectionFileIdentity:
    """把平台 stat 结果收窄成稳定的投影身份。"""
    raw_changed_ns = getattr(stat_result, "st_ctime_ns", None)
    changed_ns = (
        raw_changed_ns
        if isinstance(raw_changed_ns, int)
        else int(stat_result.st_ctime * 1_000_000_000)
    )
    return ProjectionFileIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        size=stat_result.st_size,
        modified_ns=stat_result.st_mtime_ns,
        changed_ns=changed_ns,
    )


def _metadata_bytes(metadata: dict[str, object]) -> bytes:
    """编码 audited transcript 的自包含 metadata 首行。"""
    return (
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def _is_expected_metadata(raw: bytes, expected: dict[str, object]) -> bool:
    """要求首行是与派生目录完全一致的 metadata。"""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(value, dict) and value == expected and value.get("__meta__") is True


def _preserved_item_lines(lines: list[bytes]) -> list[bytes]:
    """metadata 损坏时保留可验证 item 行，丢弃无法解释的首行。"""
    if not lines:
        return []
    try:
        value = json.loads(lines[0])
        ResponseItem.model_validate(value)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError):
        return lines[1:]
    return lines


def _atomic_rebuild(path: Path, metadata: dict[str, object], item_lines: list[bytes]) -> None:
    """在同目录临时文件中重建 metadata 后原子替换目标。"""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_metadata_bytes(metadata))
            for raw in item_lines:
                stream.write(raw.rstrip(b"\r\n") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def _decode_items(lines: list[bytes]) -> tuple[ResponseItem, ...]:
    """严格解析非空 item 行；audited snapshot 不静默跳过损坏内容。"""
    items: list[ResponseItem] = []
    for raw in lines:
        if not raw.strip():
            continue
        value = json.loads(raw)
        items.append(ResponseItem.model_validate(value))
    return tuple(items)


def _read_stable_file(path: Path) -> tuple[bytes, ProjectionFileIdentity]:
    """从一个稳定 fd 读取 bytes，并确认读取后 path 仍指向同一身份。"""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = _identity(os.fstat(fd))
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        after = _identity(os.fstat(fd))
        if before != after or _stat_identity(path) != after:
            raise ProjectionLifecycleError("projection file identity changed during scan")
        return b"".join(chunks), after
    finally:
        os.close(fd)


async def read_audited_projection_marker(
    root: Path,
    thread_id: str,
) -> AuditedProjectionMarker | None:
    """只读 JSONL 自包含首行，不加载 execution history。"""
    path = safe_thread_path(root, thread_id)
    if _stat_identity(path) is None:
        return None
    raw, _ = await anyio.to_thread.run_sync(_read_stable_file, path)
    first_line = raw.splitlines()[0] if raw else b""
    try:
        metadata = json.loads(first_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("__meta__") is not True:
        return None
    if metadata.get("thread_id") != thread_id:
        extra = metadata.get("extra")
        if isinstance(extra, dict) and extra.get("audit_required") is True:
            raise ProjectionLifecycleError("audited projection thread identity mismatch")
        return None
    return audited_projection_marker_from_extra(metadata.get("extra"))


def _existing_file_session_id(path: Path, thread_id: str) -> str | None:
    """只读现存 JSONL 自包含 header，返回完整 audited Session identity。"""
    if _stat_identity(path) is None:
        return None
    raw, _ = _read_stable_file(path)
    first_line = raw.splitlines()[0] if raw else b""
    try:
        metadata = json.loads(first_line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProjectionLifecycleError(
            "projection file has invalid audited metadata"
        ) from exc
    extra = metadata.get("extra") if isinstance(metadata, dict) else None
    session_id = extra.get("journal_session_id") if isinstance(extra, dict) else None
    schema_version = extra.get("journal_schema_version") if isinstance(extra, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("__meta__") is not True
        or metadata.get("thread_id") != thread_id
        or not isinstance(extra, dict)
        or extra.get("audit_required") is not True
        or not isinstance(session_id, str)
        or not session_id
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise ProjectionLifecycleError(
            "projection file has incomplete audited metadata"
        )
    return session_id


def _truncate_existing(
    path: Path,
    expected: ProjectionFileIdentity,
    size: int,
) -> ProjectionFileIdentity:
    """仅在 path/fd identity 仍匹配时截断未提交物理尾。"""
    flags = os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        if not _same_open_file(path, fd, expected):
            raise ProjectionLifecycleError("projection file identity changed before truncate")
        os.ftruncate(fd, size)
        os.fsync(fd)
        truncated = _identity(os.fstat(fd))
        if _stat_identity(path) != truncated:
            raise ProjectionLifecycleError("projection file identity changed during truncate")
        return truncated
    finally:
        os.close(fd)


def _repair_torn_tail(
    path: Path,
    raw: bytes,
    identity: ProjectionFileIdentity,
    metadata: dict[str, object],
) -> tuple[bytes, ProjectionFileIdentity]:
    """仅截断无换行尾；截断前严格验证 metadata 与全部完整 item 行。"""
    if not raw or raw.endswith(b"\n"):
        return raw, identity
    cutoff = raw.rfind(b"\n") + 1
    complete_lines = raw[:cutoff].splitlines()
    if not complete_lines or not _is_expected_metadata(complete_lines[0], metadata):
        return raw, identity
    _decode_items(complete_lines[1:])
    _truncate_existing(path, identity, cutoff)
    return _read_stable_file(path)


def _scan_projection_file(
    path: Path,
    metadata: dict[str, object],
) -> ProjectionSnapshot:
    """读取、修复 metadata，并返回与最终 inode 对齐的严格 snapshot。"""
    if not path.exists():
        _atomic_rebuild(path, metadata, [])
    raw, identity = _read_stable_file(path)
    raw, identity = _repair_torn_tail(path, raw, identity, metadata)
    lines = raw.splitlines()
    if not lines or not _is_expected_metadata(lines[0], metadata):
        repair_lines = lines[:-1] if raw and not raw.endswith(b"\n") else lines
        item_lines = _preserved_item_lines(repair_lines)
        _decode_items(item_lines)
        _atomic_rebuild(path, metadata, item_lines)
        raw, identity = _read_stable_file(path)
        lines = raw.splitlines()
    items = _decode_items(lines[1:])
    return ProjectionSnapshot(items=items, identity=identity)


def _stat_identity(path: Path) -> ProjectionFileIdentity | None:
    """返回当前 path identity；缺失表示 cache 必须失效。"""
    try:
        return _identity(path.stat())
    except FileNotFoundError:
        return None


def _history_was_reset(
    cached: ProjectionSnapshot | None,
    current: ProjectionFileIdentity | None,
    scanned: ProjectionSnapshot,
) -> bool:
    """只把同一 target 上已知 history 的缺失或前缀缩短视为 generation reset。"""
    if cached is None or not cached.items:
        return False
    if current is None:
        return True
    return (
        len(scanned.items) < len(cached.items)
        and cached.items[: len(scanned.items)] == scanned.items
    )


def _serialize_items(items: list[ResponseItem]) -> bytes:
    """把一个 audited batch 编码为连续 JSONL bytes。"""
    chunks = [
        json.dumps(
            item.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for item in items
    ]
    return ("\n".join(chunks) + "\n").encode()


def _same_open_file(path: Path, fd: int, expected: ProjectionFileIdentity) -> bool:
    """同时比较 fd 与 path，拒绝 snapshot 后的删除、替换或外部追加。"""
    fd_identity = _identity(os.fstat(fd))
    path_identity = _stat_identity(path)
    return fd_identity == expected and path_identity == expected


def _append_existing(
    path: Path,
    payload: bytes,
    expected: ProjectionFileIdentity,
) -> ProjectionFileIdentity:
    """仅打开既有 inode，身份一致时追加并返回新身份。"""
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        if not _same_open_file(path, fd, expected):
            raise ProjectionLifecycleError("projection file identity changed before append")
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        except BaseException:
            try:
                os.ftruncate(fd, expected.size)
                os.fsync(fd)
            except OSError:
                pass
            raise
        written = _identity(os.fstat(fd))
        if _stat_identity(path) != written:
            raise ProjectionLifecycleError("projection file identity changed during append")
        return written
    finally:
        os.close(fd)


def _acquire_target(root: Path) -> _PhysicalProjectionTarget:
    """原子获取并引用 resolved directory 对应的物理目标。"""
    resolved = root.expanduser().resolve()
    with _REGISTRY_LOCK:
        target = _TARGETS.get(resolved)
        if target is None:
            target = _PhysicalProjectionTarget(resolved)
            _TARGETS[resolved] = target
        target.references += 1
        return target


def _release_target(target: _PhysicalProjectionTarget) -> None:
    """释放 handle 引用；最后一个引用销毁 backend-bound 状态。"""
    with _REGISTRY_LOCK:
        target.references -= 1
        if target.references == 0:
            target.clear()
            _TARGETS.pop(target.root, None)


class ProjectionTargetHandle:
    """store 私有生命周期与共享物理 materialization target 的组合。"""

    def __init__(self, root: Path) -> None:
        self._target = _acquire_target(root)
        self._guard = threading.Lock()
        self._state = _HandleState.OPEN
        self._inflight = 0
        self._idle = anyio.Event()
        self._idle.set()

    @asynccontextmanager
    async def scope(self, thread_id: str) -> AsyncIterator[None]:
        """准入投影、获取共享 thread 锁，并在退出时通知 close。"""
        self._admit()
        try:
            async with self._target.lock_for(thread_id):
                yield
        finally:
            self._leave()

    @asynccontextmanager
    async def bootstrap_scope(
        self,
        thread_id: str,
        session_id: str,
    ) -> AsyncIterator[Callable[[], None]]:
        """在共享 thread 锁内 prewrite reserve Session identity。"""
        self._admit()
        try:
            async with self._target.lock_for(thread_id):
                newly_reserved = self._target.reserve_session_id(
                    thread_id,
                    session_id,
                )
                persisted = False

                def mark_persisted() -> None:
                    nonlocal persisted
                    persisted = True

                try:
                    yield mark_persisted
                finally:
                    if newly_reserved and not persisted:
                        self._target.release_session_id(thread_id, session_id)
        finally:
            self._leave()

    def _admit(self) -> None:
        """只允许 OPEN handle 准入，并绑定物理 target backend。"""
        with self._guard:
            if self._state is not _HandleState.OPEN:
                raise ProjectionLifecycleError("projection store handle is closed")
            self._target.bind_backend()
            if self._inflight == 0:
                self._idle = anyio.Event()
            self._inflight += 1

    def _leave(self) -> None:
        """释放一次准入；最后一个投影退出时唤醒 close。"""
        with self._guard:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    def state(self, thread_id: str) -> tuple[ProjectionResult | None, int | None]:
        """读取物理 target 的共享 projector state。"""
        with self._guard:
            if self._state is _HandleState.CLOSED:
                return None, None
        return self._target.states.get(thread_id, (None, None))

    def update_state(
        self,
        thread_id: str,
        result: ProjectionResult,
        blocked_seq: int | None,
    ) -> None:
        """在共享 scope 内更新物理 target state。"""
        self._target.states[thread_id] = result, blocked_seq

    def first_sequence(self, thread_id: str) -> int | None:
        """返回该物理 target 首次健康观察的 conversation seq。"""
        return self._target.first_sequences.get(thread_id)

    def record_first_sequence(self, thread_id: str, seq: int) -> None:
        """只记录一次最早健康 seq，供 generation reset 校验 replay 起点。"""
        self._target.first_sequences.setdefault(thread_id, seq)

    def expected_session_id(self, thread_id: str) -> str | None:
        """返回 physical target 已绑定的 Journal Session identity。"""
        return self._target.session_id_for(thread_id)

    def bind_expected_session_id(self, thread_id: str, session_id: str) -> None:
        """把 audited bootstrap/directory identity 绑定到 physical target。"""
        self._target.reserve_session_id(thread_id, session_id)

    async def existing_file_session_id(self, thread_id: str) -> str | None:
        """只读现存 self-contained JSONL 的 audited Session identity。"""
        path = safe_thread_path(self._target.root, thread_id)
        return await anyio.to_thread.run_sync(
            _existing_file_session_id,
            path,
            thread_id,
        )

    def replay_window(self, thread_id: str) -> ProjectionReplayWindow | None:
        """返回 generation reset 的共享 replay 窗口。"""
        return self._target.replay_windows.get(thread_id)

    def advance_replay_window(self, thread_id: str, observed_seq: int) -> None:
        """推进 replay progress；追平旧 watermark 后关闭窗口。"""
        window = self._target.replay_windows.get(thread_id)
        if window is None:
            return
        if observed_seq >= window.ceiling:
            self._target.replay_windows.pop(thread_id, None)
            return
        self._target.replay_windows[thread_id] = replace(window, progress=observed_seq)

    def _start_replay_window(
        self,
        thread_id: str,
        expected_snapshot: ProjectionSnapshot,
    ) -> None:
        """用 reset 前的 healthy snapshot 与 watermark 建立 replay 窗口。"""
        floor = self._target.first_sequences.get(thread_id)
        state = self._target.states.get(thread_id)
        if floor is None or state is None or state[0].stale:
            return
        self._target.replay_windows.setdefault(
            thread_id,
            ProjectionReplayWindow(
                floor=floor,
                ceiling=state[0].projected_seq,
                expected_items=expected_snapshot.items,
            ),
        )

    async def load_snapshot(
        self,
        thread_id: str,
        metadata: dict[str, object],
    ) -> ProjectionSnapshot:
        """用 O(1) stat 复用 cache；身份变化时在线程中严格重扫/修复。"""
        expected_session = self._target.session_id_for(thread_id)
        extra = metadata.get("extra")
        directory_session = (
            extra.get("journal_session_id") if isinstance(extra, dict) else None
        )
        if (
            expected_session is not None
            and directory_session != expected_session
        ):
            raise ProjectionLifecycleError(
                "projection directory changed Journal Session identity"
            )
        path = safe_thread_path(self._target.root, thread_id)
        cached = self._target.snapshots.get(thread_id)
        current = await anyio.to_thread.run_sync(_stat_identity, path)
        if cached is not None and current == cached.identity:
            return cached
        snapshot = await anyio.to_thread.run_sync(_scan_projection_file, path, metadata)
        snapshot = replace(
            snapshot,
            history_reset=_history_was_reset(cached, current, snapshot),
        )
        if snapshot.history_reset and cached is not None:
            self._start_replay_window(thread_id, cached)
        self._target.snapshots[thread_id] = snapshot
        self._target.scan_counts[thread_id] = self.scan_count(thread_id) + 1
        return snapshot

    async def append_batch(
        self,
        thread_id: str,
        items: list[ResponseItem],
        expected: ProjectionFileIdentity,
    ) -> None:
        """在线程中 open-existing + identity-check 追加，并增量更新 cache。"""
        path = safe_thread_path(self._target.root, thread_id)
        payload = _serialize_items(items)
        written = await anyio.to_thread.run_sync(_append_existing, path, payload, expected)
        cached = self._target.snapshots.get(thread_id)
        if cached is None or cached.identity != expected:
            self._target.snapshots.pop(thread_id, None)
            return
        self._target.snapshots[thread_id] = ProjectionSnapshot(
            items=(*cached.items, *items),
            identity=written,
            history_reset=False,
        )

    def scan_count(self, thread_id: str) -> int:
        """返回该物理 target 的完整文件扫描次数。"""
        return self._target.scan_counts.get(thread_id, 0)

    def invalidate(self, thread_id: str) -> None:
        """显式创建或修复外部 metadata 后使旧 snapshot 失效。"""
        self._target.snapshots.pop(thread_id, None)

    async def close(self) -> None:
        """拒绝新投影，等待本 handle 的已准入投影，并释放共享引用。"""
        with self._guard:
            if self._state is _HandleState.CLOSED:
                return
            if self._state is _HandleState.OPEN:
                self._state = _HandleState.CLOSING
            idle = self._idle
        await idle.wait()
        with self._guard:
            if self._state is _HandleState.CLOSED:
                return
            self._state = _HandleState.CLOSED
        _release_target(self._target)
