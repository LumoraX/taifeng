"""glob / grep 的文件访问面：同步接口，在搜索的工作线程里调用（ADR 0113）。

遍历逻辑（确定性顺序、.gitignore、排除目录、符号链接规则、停止信号）只写一份，文件访问经本模块：

- ``LocalSearchFs``：本机目录，直接用 ``os.scandir`` / ``open``，与此前的行为逐字一致；
- ``WorkspaceSearchFs``：任意 ``WorkspaceFS``。遍历跑在工作线程里，而 ``WorkspaceFS`` 是异步协议——
  每次访问经 ``run_coroutine_threadsafe`` 送回事件循环执行，工作线程等结果。事件循环此时空着
  （它在 await 这个工作线程），不会死锁。

非本机工作区里的路径以虚拟根 ``/`` 表达（``/src/a.py`` = 工作区里的 ``src/a.py``），与本机的
``Path`` 同为 ``PurePath``，遍历代码不区分。

非本机工作区的两点差异：符号链接一律跳过并计数（协议不给链接目标，无法判定是否仍在工作区内）；
``.gitignore`` 是符号链接时照常读取（越界由工作区实现自己挡）。
"""

from __future__ import annotations

import asyncio
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from taifeng.tool.builtins.gitignore import MAX_GITIGNORE_BYTES, read_gitignore
from taifeng.tool.workspace import LocalWorkspaceFS

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from taifeng.tool.workspace import WorkspaceFS

#: 工作线程等一次工作区访问的上限：事件循环没了（宿主在关闭）时线程不至于永远挂着
_BRIDGE_CALL_TIMEOUT_SECONDS = 60.0

VIRTUAL_ROOT = PurePosixPath("/")


class SearchEntry(Protocol):
    """目录项（``os.DirEntry`` 的子集）。"""

    @property
    def name(self) -> str: ...

    @property
    def path(self) -> str: ...

    def is_dir(self, *, follow_symlinks: bool = True) -> bool: ...

    def is_file(self, *, follow_symlinks: bool = True) -> bool: ...

    def is_symlink(self) -> bool: ...


class SearchFs(Protocol):
    """搜索遍历需要的全部文件访问。方法都是同步的，失败抛 ``OSError``。"""

    def is_file(self, path: PurePath) -> bool:
        """是否为普通文件（跟随符号链接）。"""
        ...

    def scandir(self, directory: PurePath) -> list[SearchEntry]:
        """列目录项（顺序不限，调用方排序）。"""
        ...

    def symlink_file_inside(self, entry: SearchEntry) -> bool:
        """符号链接解析后是否为工作区内的普通文件。"""
        ...

    def read_limited(self, path: PurePath, max_bytes: int) -> bytes | None:
        """读整个文件；大于 ``max_bytes`` 返回 None（不读进内存）。"""
        ...

    def read_gitignore(self, directory: PurePath) -> str | None:
        """读 ``directory/.gitignore``；没有返回 None，读不了 / 超大 / 非 UTF-8 抛 ``OSError``。"""
        ...


class LocalSearchFs:
    """本机目录。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def is_file(self, path: PurePath) -> bool:
        return Path(path).is_file()

    def scandir(self, directory: PurePath) -> list[SearchEntry]:
        with os.scandir(directory) as entries:
            return list(entries)

    def symlink_file_inside(self, entry: SearchEntry) -> bool:
        """与 file_read 的沙盒判定同语义。"""
        try:
            target = Path(entry.path).resolve(strict=False)
        except OSError:
            return False
        inside = target == self._root or self._root in target.parents
        return inside and target.is_file()

    def read_limited(self, path: PurePath, max_bytes: int) -> bytes | None:
        with Path(path).open("rb") as handle:
            # 多读 1 字节判断是否超限，避免先 stat 再读的竞态
            data = handle.read(max_bytes + 1)
        return None if len(data) > max_bytes else data

    def read_gitignore(self, directory: PurePath) -> str | None:
        return read_gitignore(Path(directory))


@dataclass(frozen=True)
class _WorkspaceEntryView:
    """把 ``WorkspaceEntry`` 适配成目录项。"""

    name: str
    path: str
    directory: bool
    file: bool
    symlink: bool

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self.directory

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return self.file

    def is_symlink(self) -> bool:
        return self.symlink


class WorkspaceSearchFs:
    """任意 ``WorkspaceFS``：访问送回事件循环执行。"""

    def __init__(self, workspace: WorkspaceFS, loop: asyncio.AbstractEventLoop) -> None:
        self._workspace = workspace
        self._loop = loop

    def _run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """在事件循环上执行一次工作区访问，工作线程等结果。"""
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=_BRIDGE_CALL_TIMEOUT_SECONDS)
        except TimeoutError:
            future.cancel()
            raise

    @staticmethod
    def _relative(path: PurePath) -> str:
        """虚拟根下的路径 → 相对工作区根的路径（根本身是空串）。"""
        relative = PurePosixPath(path).relative_to(VIRTUAL_ROOT).as_posix()
        return "" if relative == "." else relative

    def is_file(self, path: PurePath) -> bool:
        return bool(self._run(self._workspace.metadata(self._relative(path))).is_file)

    def scandir(self, directory: PurePath) -> list[SearchEntry]:
        entries = self._run(self._workspace.list_directory(self._relative(directory)))
        return [
            _WorkspaceEntryView(
                name=entry.name, path=posixpath.join(str(directory), entry.name),
                directory=entry.is_directory, file=entry.is_file, symlink=entry.is_symlink,
            )
            for entry in entries
        ]

    def symlink_file_inside(self, entry: SearchEntry) -> bool:
        return False

    def read_limited(self, path: PurePath, max_bytes: int) -> bytes | None:
        relative = self._relative(path)
        if self._run(self._workspace.metadata(relative)).size > max_bytes:
            return None
        data: bytes = self._run(self._workspace.read_bytes(relative))
        return None if len(data) > max_bytes else data

    def read_gitignore(self, directory: PurePath) -> str | None:
        target = directory / ".gitignore"
        if not self._run(self._workspace.metadata(self._relative(target))).is_file:
            return None
        data = self.read_limited(target, MAX_GITIGNORE_BYTES)
        if data is None:
            raise OSError(f"{target} larger than {MAX_GITIGNORE_BYTES} bytes")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OSError(f"{target} is not valid UTF-8") from exc


def virtual_path(root: str, resolved: str) -> PurePosixPath:
    """工作区规范路径 → 虚拟根下的路径。

    依赖 ``WorkspaceFS.resolve`` 的约定：返回值是 ``root`` 本身或以 ``root + "/"`` 开头。

    Raises:
        ValueError: 规范路径不以工作区根为前缀（实现违反了协议约定）。
    """
    if resolved == root:
        return VIRTUAL_ROOT
    prefix = root.rstrip("/") + "/"
    if not resolved.startswith(prefix):
        raise ValueError(f"workspace path {resolved!r} is not under its root {root!r}")
    return VIRTUAL_ROOT / resolved[len(prefix):]


@dataclass(frozen=True)
class SearchScope:
    """一次搜索的范围。

    Attributes:
        fs: 遍历用的文件访问面。
        root: 遍历根（本机是工作区根目录，非本机是虚拟根）。
        base: 搜索基点，与 ``root`` 同一种路径。
        target: 基点在工作区里的规范路径（权限 target）。
    """

    fs: SearchFs
    root: PurePath
    base: PurePath
    target: str


def search_scope(workspace: WorkspaceFS, relative: str) -> SearchScope:
    """按工作区种类选文件访问面，并把搜索基点换成遍历用的路径。须在事件循环里调用。

    Raises:
        WorkspacePathError: 基点落在工作区之外。
    """
    target = workspace.resolve(relative)
    if isinstance(workspace, LocalWorkspaceFS):
        root = workspace.root_path
        return SearchScope(LocalSearchFs(root), root, Path(target), target)
    return SearchScope(
        WorkspaceSearchFs(workspace, asyncio.get_running_loop()),
        VIRTUAL_ROOT, virtual_path(workspace.root, target), target,
    )


__all__ = [
    "VIRTUAL_ROOT",
    "LocalSearchFs",
    "SearchEntry",
    "SearchFs",
    "SearchScope",
    "WorkspaceSearchFs",
    "search_scope",
    "virtual_path",
]
