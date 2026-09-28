"""SessionJournal core 的同步文件 IO 可注入边界。

core 统一把这些同步调用派发到线程池；测试可注入实现观察 / 故障注入 mutation 前后。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from taifeng.conversation.journal.errors import CommitNotStartedError

if TYPE_CHECKING:
    from pathlib import Path


class SyncFileAdapter(Protocol):
    """注入同步文件边界，core 统一在线程池调用。"""

    def create_exclusive(self, path: Path, payload: bytes) -> None:
        """独占创建、写入并 fsync 文件及父目录。"""

    def read_bytes(self, path: Path) -> bytes:
        """读取完整物理 Journal bytes。"""

    def append_durable(self, path: Path, payload: bytes) -> None:
        """追加并 fsync；明确未 mutation 时用 CommitNotStartedError。"""


class DefaultSyncFileAdapter:
    """基于本地文件系统的 durable 同步适配器。"""

    def create_exclusive(self, path: Path, payload: bytes) -> None:
        """以 ``xb`` 独占创建，并在返回前完成 file/directory fsync。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("xb")
        except OSError as exc:
            raise CommitNotStartedError(exc) from None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def read_bytes(self, path: Path) -> bytes:
        """读取物理文件；不存在时由调用方决定语义。"""
        return path.read_bytes()

    def append_durable(self, path: Path, payload: bytes) -> None:
        """追加 batch，并在返回前完成 file flush+fsync。"""
        try:
            stream = path.open("ab")
        except OSError as exc:
            raise CommitNotStartedError(exc) from None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())


__all__ = ["DefaultSyncFileAdapter", "SyncFileAdapter"]
