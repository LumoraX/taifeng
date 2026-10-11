"""WorkspaceFS 行为一致性检查，不依赖本机路径或 pytest（ADR 0129）。

每个 case 需要隔离的新存储，harness.new_workspace 返回该存储上的实现。
宿主负责连接等资源的最终清理。协议不提供链接创建和物理故障注入，这些由实现专测。
未承诺原子写的后端可明确跳过 atomic_writes，其余检查仍必须通过。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import anyio

from taifeng.testing.journal_conformance import ConformanceCase, ConformanceFailure, _check, _expect
from taifeng.tool.workspace import WorkspacePathError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.tool.workspace import WorkspaceFS


class WorkspaceHarness(Protocol):
    """单项检查的接入点，一份 harness 对应独立工作区。"""

    async def new_workspace(self) -> WorkspaceFS:
        """返回该检查使用的工作区，不能与其他检查共享文件。"""
        ...


async def _paths(harness: WorkspaceHarness) -> None:
    """规范路径可重新解析并访问同一文件。"""
    fs = await harness.new_workspace()
    _check(fs.resolve("") == fs.root, "empty path must resolve to workspace root")
    _check(fs.resolve(".") == fs.root, "dot path must resolve to workspace root")
    resolved = fs.resolve("a/../data")
    _check(resolved.startswith(fs.root.rstrip("/") + "/"), "canonical path must remain inside root")
    _check(fs.resolve(resolved) == resolved, "resolve must be idempotent")
    await fs.write_bytes(resolved, b"canonical")
    _check(await fs.read_bytes("data") == b"canonical", "canonical and relative paths must agree")


async def _boundary(harness: WorkspaceHarness) -> None:
    """所有方法独立拒绝越界，不能只在 resolve 上设防。"""
    fs = await harness.new_workspace()
    for path in ("..", "../outside", "a/../../outside"):
        try:
            fs.resolve(path)
        except WorkspacePathError:
            continue
        _check(False, "resolve must reject workspace escapes")
    calls: list[Callable[[], Awaitable[object]]] = [
        lambda: fs.read_bytes("../outside"),
        lambda: fs.write_bytes("../outside", b"escape"),
        lambda: fs.metadata("../outside"),
        lambda: fs.list_directory(".."),
        lambda: fs.remove("../outside"),
    ]
    for call in calls:
        await _expect(WorkspacePathError, call(), "each workspace method must reject escapes")


async def _bytes(harness: WorkspaceHarness) -> None:
    """空内容、UTF-8 与任意二进制完整往返，元数据随覆盖更新。"""
    fs = await harness.new_workspace()
    for data in (b"", "你好".encode(), bytes(range(256))):
        await fs.write_bytes("data", data)
        _check(await fs.read_bytes("data") == data, "read bytes must equal written bytes")
        info = await fs.metadata("data")
        _check(info.exists and info.is_file and not info.is_directory,
               "written file must have file metadata")
        _check(info.size == len(data), "metadata size must match byte count")
    await fs.remove("data")
    _check(not (await fs.metadata("data")).exists, "removed file must not exist")


async def _missing(harness: WorkspaceHarness) -> None:
    """不存在及禁止建父目录的错误语义。"""
    fs = await harness.new_workspace()
    _check(not (await fs.metadata("missing")).exists, "missing metadata must be exists=False")
    await _expect(FileNotFoundError, fs.read_bytes("missing"), "missing read")
    await _expect(FileNotFoundError, fs.remove("missing"), "missing remove")
    await _expect(FileNotFoundError, fs.list_directory("missing"), "missing directory")
    await _expect(FileNotFoundError,
                  fs.write_bytes("missing/data", b"x", create_parents=False),
                  "write without existing parent")
    _check(not (await fs.metadata("missing/data")).exists, "failed write must not create target")


async def _directories(harness: WorkspaceHarness) -> None:
    """父目录创建、直接条目与非空目录删除。"""
    fs = await harness.new_workspace()
    await fs.write_bytes("parent/deep/data", b"nested")
    _check((await fs.metadata("parent")).is_directory, "write must create parent directories")
    entries = await fs.list_directory("parent")
    _check(len(entries) == 1 and entries[0].name == "deep", "listing must contain direct children only")
    _check(entries[0].is_directory and not entries[0].is_file,
           "directory entry must identify a directory")
    await _expect(OSError, fs.remove("parent"), "nonrecursive removal of nonempty directory")
    _check(await fs.read_bytes("parent/deep/data") == b"nested", "refused remove must preserve content")
    await fs.remove("parent", recursive=True)
    _check(not (await fs.metadata("parent")).exists, "recursive removal must remove descendants")


async def _atomic_writes(harness: WorkspaceHarness) -> None:
    """并发调用不共享临时文件，读者始终只见完整版本。"""
    fs = await harness.new_workspace()
    versions = {bytes([index]) * 65536 for index in range(8)}
    await fs.write_bytes("data", b"initial")
    allowed = versions | {b"initial"}

    complete = True

    async def read_versions() -> None:
        """在组外断言读到的版本，避免断言被包装成 ExceptionGroup。"""
        nonlocal complete
        for _ in range(50):
            complete = (await fs.read_bytes("data") in allowed) and complete
            await anyio.sleep(0)

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(read_versions)
            for value in versions:
                group.start_soon(fs.write_bytes, "data", value)
    except ExceptionGroup as error:
        # 普通后端失败归一到具名报告；取消属 BaseException，不在这里吞掉。
        raise ConformanceFailure("atomic workspace operations failed") from error
    _check(complete, "atomic write exposed partial bytes")
    _check(await fs.read_bytes("data") in versions, "final content must be a complete written version")
    entries = await fs.list_directory("")
    _check(len(entries) == 1 and entries[0].name == "data", "writes must leave no temporary entries")


def workspace_fs_cases() -> tuple[ConformanceCase, ...]:
    """返回7成员协议的具名行为检查，供本机、容器与远端后端复用。"""
    return (
        ConformanceCase("paths", "规范与相对路径的一致性", _paths),
        ConformanceCase("boundary", "逐方法拒绝越界", _boundary),
        ConformanceCase("bytes", "空、文本与二进制往返", _bytes),
        ConformanceCase("missing", "不存在与禁止创建父目录", _missing),
        ConformanceCase("directories", "目录条目与删除", _directories),
        ConformanceCase("atomic_writes", "并发写与完整版本可见性", _atomic_writes),
    )


__all__ = ["WorkspaceHarness", "workspace_fs_cases"]
