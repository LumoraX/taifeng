"""工作区一致性套件的参考后端与故障检出。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng import LocalWorkspaceFS
from taifeng.testing import ConformanceFailure

if TYPE_CHECKING:
    from pathlib import Path


class Harness:
    """为每项检查提供独立的本机工作区。"""

    def __init__(self, root: Path) -> None:
        """保存 pytest 提供的隔离目录。"""
        self.root = root

    async def new_workspace(self) -> LocalWorkspaceFS:
        """创建参考实现。"""
        self.root.mkdir(parents=True, exist_ok=True)
        return LocalWorkspaceFS(self.root)


@pytest.mark.parametrize("name", ["paths", "boundary", "bytes", "missing", "directories", "atomic_writes"])
async def test_reference_backend_passes(name: str, tmp_path: Path) -> None:
    """具名套件验证参考实现，而不只检查接口存在。"""
    from taifeng.testing import workspace_fs_cases

    case = next(case for case in workspace_fs_cases() if case.name == name)
    await case.run(Harness(tmp_path / "work"))


async def test_corrupt_read_backend_is_detected(tmp_path: Path) -> None:
    """读写不一致的第三方后端会被套件检出。"""
    from taifeng.testing import workspace_fs_cases

    class CorruptFS(LocalWorkspaceFS):
        """模拟总是丢掉文件内容的实现。"""

        async def read_bytes(self, path: str) -> bytes:
            """返回错误内容。"""
            return b"wrong"

    class CorruptHarness(Harness):
        """注入损坏实现。"""

        async def new_workspace(self) -> LocalWorkspaceFS:
            """为字节往返检查提供坏后端。"""
            self.root.mkdir()
            return CorruptFS(self.root)

    case = next(case for case in workspace_fs_cases() if case.name == "bytes")
    with pytest.raises(ConformanceFailure, match="bytes"):
        await case.run(CorruptHarness(tmp_path / "work"))


async def test_bad_atomic_backend_is_reported_by_run_cases(tmp_path: Path) -> None:
    """并发检查的失败返回报告，不能以 ExceptionGroup 中断验收。"""
    from taifeng.testing import run_cases, workspace_fs_cases

    class PartialFS(LocalWorkspaceFS):
        """模拟向读者暴露半截写入的后端。"""

        async def read_bytes(self, path: str) -> bytes:
            """任意读取返回错误的半截内容。"""
            return b"partial"

    class PartialHarness(Harness):
        """提供错误后端的隔离目录。"""

        async def new_workspace(self) -> LocalWorkspaceFS:
            """创建目录并返回错误实现。"""
            self.root.mkdir()
            return PartialFS(self.root)

    cases = tuple(case for case in workspace_fs_cases() if case.name == "atomic_writes")
    failures = await run_cases(cases, lambda: PartialHarness(tmp_path / "work"))
    assert "atomic_writes" in failures
    assert "partial" in failures["atomic_writes"]


async def test_concurrent_backend_io_failure_is_reported(tmp_path: Path) -> None:
    """并发写的底层错误也归一为一致性报告。"""
    from taifeng.testing import run_cases, workspace_fs_cases

    class BrokenFS(LocalWorkspaceFS):
        """初始化可写，之后写入失败。"""

        async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
            """模拟独占临时文件实现不正确而导致的并发错误。"""
            if data != b"initial":
                raise OSError("concurrent write failed")
            await super().write_bytes(path, data, create_parents=create_parents)

    class BrokenHarness(Harness):
        """返回失败实现。"""

        async def new_workspace(self) -> LocalWorkspaceFS:
            """使用单独的测试目录。"""
            self.root.mkdir()
            return BrokenFS(self.root)

    cases = tuple(case for case in workspace_fs_cases() if case.name == "atomic_writes")
    failures = await run_cases(cases, lambda: BrokenHarness(tmp_path / "work"))
    assert "operations failed" in failures["atomic_writes"]


async def test_conformance_cancellation_is_not_a_failure_report(tmp_path: Path) -> None:
    """取消整个验收时原样传播取消，不混成后端失败。"""
    import asyncio

    from taifeng.testing import run_cases, workspace_fs_cases

    entered = asyncio.Event()

    class PausedFS(LocalWorkspaceFS):
        """停住读取以取消并发检查。"""

        async def read_bytes(self, path: str) -> bytes:
            """等待外部取消。"""
            entered.set()
            await asyncio.Event().wait()
            return b""

    class PausedHarness(Harness):
        """提供可取消的后端。"""

        async def new_workspace(self) -> LocalWorkspaceFS:
            """创建隔离存储。"""
            self.root.mkdir()
            return PausedFS(self.root)

    cases = tuple(case for case in workspace_fs_cases() if case.name == "atomic_writes")
    task = asyncio.create_task(run_cases(cases, lambda: PausedHarness(tmp_path / "work")))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
