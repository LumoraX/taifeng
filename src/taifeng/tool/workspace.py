"""WorkspaceFS —— 文件类工具读写文件的唯一 seam（ADR 0113）。

``shell_exec`` 经 ``CommandExecutor`` 把命令放进容器或远端沙盒之后，文件类工具如果还直接读写宿主机
的目录，模型就会看到两个对不上的世界：命令在沙盒里改了文件，``file_read`` 读到的却是宿主机上的旧
内容。``WorkspaceFS`` 把「工作区里的文件」抽成协议：内核只定协议 + 本机默认实现，容器 / 远端实现
由宿主注入（ADR 0017 规则③，与 ``CommandExecutor`` 同一范式）。

协议只管文件访问。路径规则、权限审批、大小上限、分页、补丁的先校验后应用仍由工具统一负责，换
实现不改变这些语义。

路径约定：
    - 工具拿到的是相对工作区根的路径；``resolve`` 把它规范成这个工作区里的规范路径，落在工作区之外
      抛 ``WorkspacePathError``。规范路径用于权限 target 与结果回显；
    - 其余方法既接受相对路径、也接受 ``resolve`` 返回的规范路径，并且**各自再校验一次**不越界——
      实现不得假设调用方先调过 ``resolve``；
    - 失败以标准的 ``OSError`` 子类表达：不存在 ``FileNotFoundError``、越界或无权 ``PermissionError``
      （``WorkspacePathError`` 是它的子类）、其余 ``OSError``。

参照：deepagents 的 sandbox backend（文件操作随后端走）；差异：协议只有 7 个成员，不含执行。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import anyio.to_thread


class WorkspacePathError(PermissionError):
    """路径落在工作区之外。"""


@dataclass(frozen=True)
class WorkspaceFileInfo:
    """文件或目录的元数据；不存在时只有 ``exists=False``。"""

    exists: bool
    is_directory: bool = False
    is_file: bool = False
    size: int = 0
    modified_at: float = 0.0


@dataclass(frozen=True)
class WorkspaceEntry:
    """目录下的一个条目（不跟随符号链接判定类型）。"""

    name: str
    is_directory: bool
    is_file: bool
    is_symlink: bool = False


@runtime_checkable
class WorkspaceFS(Protocol):
    """工作区文件访问协议。"""

    @property
    def root(self) -> str:
        """工作区根的标识：给模型看的工具描述与权限规则里出现的就是它。"""
        ...

    def resolve(self, path: str) -> str:
        """把相对工作区根的路径规范成规范路径（不做文件读写）。

        Raises:
            WorkspacePathError: 路径落在工作区之外。
        """
        ...

    async def read_bytes(self, path: str) -> bytes:
        """读取整个文件。

        Raises:
            FileNotFoundError: 不存在。
            OSError: 不是普通文件、越界或读取失败。
        """
        ...

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        """覆盖写入。应当是原子的（读者看到旧内容或新内容）；做不到的实现须在自己的文档里说明。

        Raises:
            FileNotFoundError: 父目录不存在且 ``create_parents=False``。
            OSError: 越界或写入失败。
        """
        ...

    async def metadata(self, path: str) -> WorkspaceFileInfo:
        """取元数据；不存在时返回 ``exists=False``，不抛异常。

        Raises:
            OSError: 越界或查询失败。
        """
        ...

    async def list_directory(self, path: str) -> list[WorkspaceEntry]:
        """列目录（不递归）。顺序不作要求，调用方自行排序。

        Raises:
            FileNotFoundError: 不存在。
            OSError: 不是目录、越界或读取失败。
        """
        ...

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """删除文件；目录在 ``recursive=False`` 时须为空。

        Raises:
            FileNotFoundError: 不存在。
            OSError: 目录非空且未要求递归、越界或删除失败。
        """
        ...


def resolve_local(root: Path, requested: str) -> Path | None:
    """本机路径解析：跟随符号链接后仍须落在 ``root`` 之内，否则返回 None。"""
    try:
        resolved = (root / requested).resolve(strict=False)
    except OSError:
        return None
    if root not in resolved.parents and resolved != root:
        return None
    return resolved


class LocalWorkspaceFS:
    """默认实现：本机目录。

    阻塞的文件操作放到工作线程，不占事件循环；写入走「临时文件 + rename」，是原子的。
    """

    def __init__(self, root_dir: str | Path) -> None:
        """
        Args:
            root_dir: 工作区根目录；符号链接与 ``~`` 在此解析一次。
        """
        self._root = Path(root_dir).expanduser().resolve()

    @property
    def root(self) -> str:
        """根目录的绝对路径。"""
        return str(self._root)

    @property
    def root_path(self) -> Path:
        """根目录（``Path``）；本机专有，供需要同步遍历的搜索工具使用。"""
        return self._root

    def resolve(self, path: str) -> str:
        """解析成绝对路径；符号链接跟随后越界同样拒绝。"""
        return str(self._inside(path))

    def _inside(self, path: str) -> Path:
        resolved = resolve_local(self._root, path)
        if resolved is None:
            raise WorkspacePathError(f"{path} is outside the workspace ({self._root})")
        return resolved

    async def read_bytes(self, path: str) -> bytes:
        """读取整个文件。"""
        return await anyio.to_thread.run_sync(self._inside(path).read_bytes)

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        """写临时文件后原子替换。"""
        target = self._inside(path)

        def write() -> None:
            if create_parents:
                target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)

        await anyio.to_thread.run_sync(write)

    async def metadata(self, path: str) -> WorkspaceFileInfo:
        """跟随符号链接取元数据；不存在返回 ``exists=False``。"""
        target = self._inside(path)

        def stat() -> WorkspaceFileInfo:
            try:
                info = target.stat()
            except (FileNotFoundError, NotADirectoryError):
                return WorkspaceFileInfo(exists=False)
            return WorkspaceFileInfo(
                exists=True, is_directory=target.is_dir(), is_file=target.is_file(),
                size=info.st_size, modified_at=info.st_mtime,
            )

        return await anyio.to_thread.run_sync(stat)

    async def list_directory(self, path: str) -> list[WorkspaceEntry]:
        """列目录项（不跟随符号链接判定类型）。"""
        target = self._inside(path)

        def scan() -> list[WorkspaceEntry]:
            with os.scandir(target) as entries:
                return [
                    WorkspaceEntry(
                        name=entry.name,
                        is_directory=entry.is_dir(follow_symlinks=False),
                        is_file=entry.is_file(follow_symlinks=False),
                        is_symlink=entry.is_symlink(),
                    )
                    for entry in entries
                ]

        return await anyio.to_thread.run_sync(scan)

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """删文件；目录非递归时须为空。"""
        target = self._inside(path)

        def delete() -> None:
            if target.is_dir() and not target.is_symlink():
                if recursive:
                    shutil.rmtree(target)
                else:
                    target.rmdir()
            else:
                target.unlink()

        await anyio.to_thread.run_sync(delete)


def workspace_for(root_dir: str | Path | None, workspace: WorkspaceFS | None) -> WorkspaceFS:
    """工具工厂的入参归一：``root_dir`` 与 ``workspace`` 恰好给一个。

    Raises:
        ValueError: 两个都给或都不给。
        TypeError: ``workspace`` 不满足 ``WorkspaceFS``。
    """
    if (root_dir is None) == (workspace is None):
        raise ValueError("exactly one of root_dir / workspace is required")
    if workspace is None:
        assert root_dir is not None
        return LocalWorkspaceFS(root_dir)
    if not isinstance(workspace, WorkspaceFS):
        raise TypeError(f"workspace must implement WorkspaceFS, got {type(workspace).__name__}")
    return workspace


__all__ = [
    "LocalWorkspaceFS",
    "WorkspaceEntry",
    "WorkspaceFS",
    "WorkspaceFileInfo",
    "WorkspacePathError",
    "resolve_local",
    "workspace_for",
]
