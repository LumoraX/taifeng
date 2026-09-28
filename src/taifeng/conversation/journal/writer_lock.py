"""SessionJournal 跨进程写者互斥：OS 级建议锁边界（Phase 2）。

同进程 fencing 只靠 core 实例内的 ``_writers`` 字典，挡不住另一个进程 / 另一个 core
实例对同一 Session 文件追加。本模块把「一个 Session 同一时刻只有一个 writer」落到
OS 级 ``flock(LOCK_EX | LOCK_NB)``：锁文件为 ``<session>.journal.lock``，fd 从获取一直
持有到 ``close_session`` / ``close`` 释放；进程崩溃时内核自动释放锁，接管方无需清理。

锁获取 / 释放经 :class:`WriterLockAdapter` 可注入边界（与 ``SyncFileAdapter`` 同风格，
同步实现、由 core 统一派发到线程池），测试可替换为内存实现观察调用。

注意：锁文件**永不删除**。若释放时删除，另一个进程可能在删除前已 open 旧 inode、
删除后又有第三方 open 新 inode，两者各自 flock 成功 → 互斥失效。
"""

from __future__ import annotations

import os
import sys
import weakref
from typing import TYPE_CHECKING, Protocol

from taifeng.conversation.journal.errors import JournalLockUnsupportedError

if TYPE_CHECKING:
    from pathlib import Path


def _posix_flock_available() -> bool:
    """当前平台是否提供 POSIX ``fcntl.flock``（独立成函数便于测试模拟非 POSIX）。"""
    return os.name == "posix"


class WriterLockBusyError(Exception):
    """锁已被其他进程 / 其他打开的文件描述持有（core 映射为 ``JournalBusyError``）。"""

    def __init__(self, path: Path) -> None:
        """只记录锁路径，不携带底层 errno 文本。"""
        super().__init__(f"journal writer lock busy: {path.name}")
        self.path = path


class WriterLockAdapter(Protocol):
    """注入跨进程写者锁的同步边界；core 统一在线程池调用。"""

    def acquire(self, path: Path) -> object:
        """非阻塞独占获取锁并返回不透明 handle；已被占用抛 ``WriterLockBusyError``。"""
        ...

    def release(self, handle: object) -> None:
        """释放 ``acquire`` 返回的 handle；同一 handle 只释放一次。"""
        ...


class _FlockHandle:
    """默认实现持有的锁文件描述符。

    fd 生命周期绑定到 handle 对象：显式 ``release`` 之外，handle 被回收（持有它的
    core 被丢弃）时 ``weakref.finalize`` 兜底关闭 fd，等价于进程退出时内核释放锁，
    避免 fd 泄漏把锁永久占住。
    """

    def __init__(self, path: Path, fd: int) -> None:
        """登记 fd 与一次性关闭 finalizer。"""
        self.path = path
        self.fd = fd
        self._finalizer = weakref.finalize(self, os.close, fd)

    @property
    def released(self) -> bool:
        """fd 是否已关闭（锁已释放）。"""
        return not self._finalizer.alive

    def close(self) -> None:
        """关闭 fd（幂等由 finalizer 保证只执行一次）。"""
        self._finalizer()


class FcntlWriterLockAdapter:
    """基于 POSIX ``fcntl.flock`` 的默认跨进程写者锁。

    ``flock`` 以「打开的文件描述」为单位互斥：同进程内两个 core 实例各自 open 锁文件
    也会互斥，因此与多进程部署语义一致。非 POSIX 平台显式抛
    ``JournalLockUnsupportedError``，绝不静默退化为不加锁。
    """

    def acquire(self, path: Path) -> object:
        """打开（必要时创建）锁文件并 ``LOCK_EX | LOCK_NB``。"""
        if not _posix_flock_available():
            raise JournalLockUnsupportedError(sys.platform)
        import fcntl

        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # 锁被他人持有：关闭本次打开的描述后以稳定类型上抛
            os.close(fd)
            raise WriterLockBusyError(path) from None
        except BaseException:
            os.close(fd)
            raise
        return _FlockHandle(path, fd)

    def release(self, handle: object) -> None:
        """解锁并关闭 fd；关闭 fd 本身也会释放 flock，``LOCK_UN`` 只是显式化。"""
        if not isinstance(handle, _FlockHandle):
            raise TypeError("handle was not produced by FcntlWriterLockAdapter")
        if handle.released:
            raise RuntimeError("journal writer lock handle already released")
        import fcntl

        try:
            fcntl.flock(handle.fd, fcntl.LOCK_UN)
        finally:
            handle.close()


__all__ = [
    "FcntlWriterLockAdapter",
    "WriterLockAdapter",
    "WriterLockBusyError",
]
