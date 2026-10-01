"""命令执行器协议 —— shell 类工具派生子进程的唯一 seam（sandbox-seam）。

``shell_exec`` 与 ``run_in_background`` 此前直接 ``asyncio.create_subprocess_*``，宿主想把
命令放进 Docker / firejail / 远端沙箱只能整个重写工具。现在两者都经 ``CommandExecutor``
启动进程：内核只定协议 + 本机默认实现，隔离实现由宿主注入（ADR 0017 规则③；与
``ScriptExecutor`` 同一范式，参照 deepagents 的 sandbox backend / codex ``sandboxing``）。

协议只管「启动」：返回的进程对象提供 ``communicate`` / ``kill`` / ``wait`` / ``returncode``——
``asyncio.subprocess.Process`` 天然满足，远端实现包一层即可。超时、取消、输出截断、
权限审批仍由工具统一负责，保证换执行器不改变这些语义。

``kill`` 的约定是「这条命令连同它派生的进程一起结束」（ADR 0108）。只杀 shell 本身不够：
``sh -c "a; b"`` 里 shell 会 fork 出子进程并把 stdout / stderr 留给它，shell 死了子进程还占着
管道——输出收不完，调用方等不到结束。本机实现让命令自成一个进程组、按组终止
（与 ``skill/scripts/shell.py`` 同一手法）。

需要与进程持续对话的调用方（MCP stdio server，ADR 0112）在 ``CommandSpec`` 里要 ``stdin=True``，
执行器返回的进程对象须满足 ``StreamingCommandProcess``：在 ``CommandProcess`` 之上提供
``stdin`` / ``stdout`` / ``stderr`` 三个流。只会一次性收输出的执行器照旧只实现 ``CommandProcess``，
它们不能用来跑这类进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CommandSpec:
    """一次命令执行请求。

    Attributes:
        command: 命令文本。
        shell: True → 交给 shell 解释（管道 / 重定向可用）；False → ``shlex.split`` 后直接 exec。
        cwd: 工作目录（None = 执行器默认）。
        env: 完整环境变量（工具已按白名单构造，执行器不得再合并宿主环境）。
        stdin: True → 调用方要向进程的标准输入持续写入，执行器返回的进程须满足
            ``StreamingCommandProcess``；False（默认）→ 进程的标准输入是空的（读到 EOF），
            不继承宿主进程的标准输入。
    """

    command: str
    shell: bool
    cwd: str | None
    env: dict[str, str]
    stdin: bool = False


@runtime_checkable
class CommandProcess(Protocol):
    """已启动进程的最小接口（``asyncio.subprocess.Process`` 天然满足）。"""

    @property
    def returncode(self) -> int | None:
        """退出码；未结束为 None。"""
        ...

    async def communicate(self) -> tuple[bytes, bytes]:
        """读完 stdout / stderr 并等待退出。"""
        ...

    def kill(self) -> None:
        """强制终止这条命令连同它派生的进程；已经结束的进程上调用不报错。"""
        ...

    async def wait(self) -> int:
        """等待退出，返回退出码。"""
        ...


@runtime_checkable
class CommandInput(Protocol):
    """进程标准输入的写入端（``asyncio.StreamWriter`` 天然满足）。"""

    def write(self, data: bytes) -> None:
        """写入缓冲；配合 ``drain`` 施加背压。"""
        ...

    async def drain(self) -> None:
        """等缓冲写出。"""
        ...

    def close(self) -> None:
        """关闭写入端（进程读到 EOF）。"""
        ...

    def is_closing(self) -> bool:
        """是否已关闭或正在关闭。"""
        ...


@runtime_checkable
class CommandOutput(Protocol):
    """进程标准输出 / 标准错误的读取端（``asyncio.StreamReader`` 天然满足）。"""

    async def readline(self) -> bytes:
        """读一行（含换行符）；流结束返回空字节串。"""
        ...

    async def read(self, n: int = -1) -> bytes:
        """读至多 ``n`` 字节；流结束返回空字节串。"""
        ...


@runtime_checkable
class StreamingCommandProcess(CommandProcess, Protocol):
    """能持续对话的进程：在 ``CommandProcess`` 之上提供三个流。

    ``CommandSpec.stdin=True`` 时执行器须返回这种对象，且 ``stdin`` / ``stdout`` 不为 None。
    """

    @property
    def stdin(self) -> CommandInput | None:
        """标准输入的写入端；没有接管道时为 None。"""
        ...

    @property
    def stdout(self) -> CommandOutput | None:
        """标准输出的读取端。"""
        ...

    @property
    def stderr(self) -> CommandOutput | None:
        """标准错误的读取端。"""
        ...


@runtime_checkable
class CommandExecutor(Protocol):
    """命令执行器：按 ``CommandSpec`` 启动进程。

    实现方约束：
        - SHALL 使用 ``spec.env`` 作为完整环境（不叠加宿主 ``os.environ``，防凭据泄漏）；
        - SHALL 让 stdout / stderr 可经 ``communicate`` 读取；
        - 启动失败 SHALL 抛 ``OSError``（工具据此返回 ``spawn_error``）。
    """

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """启动进程并立即返回（不等待结束）。"""
        ...


class _LocalProcess:
    """本机子进程：自成一个进程组，``kill`` 杀整个组。

    ``start_new_session=True`` 启动，进程的 PID 即进程组 ID。只要组里还有成员，这个 ID 就不会被
    系统挪作他用，所以 shell 本身已经退出、子进程还在时照样能按组杀到。
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._collected = False

    @property
    def returncode(self) -> int | None:
        """退出码；未结束为 None。"""
        return self._proc.returncode

    @property
    def stdin(self) -> CommandInput | None:
        """标准输入的写入端；``CommandSpec.stdin=False`` 时为 None。"""
        return self._proc.stdin

    @property
    def stdout(self) -> CommandOutput | None:
        """标准输出的读取端。"""
        return self._proc.stdout

    @property
    def stderr(self) -> CommandOutput | None:
        """标准错误的读取端。"""
        return self._proc.stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        """读完 stdout / stderr 并等待退出。"""
        output = await self._proc.communicate()
        # 管道读到 EOF 且进程已退出：没有成员还需要杀，此后 kill 不再发信号
        self._collected = True
        return output

    def kill(self) -> None:
        """SIGKILL 整个进程组；输出已收完的进程上是空操作。"""
        if self._collected:
            return
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # 进程组已不存在 / 没权限按组杀：退回只杀主进程
            if self._proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()

    async def wait(self) -> int:
        """等待退出，返回退出码。"""
        return await self._proc.wait()


class LocalCommandExecutor:
    """默认执行器：本机子进程，每条命令自成一个进程组。"""

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """``shell=True`` 走 ``create_subprocess_shell``，否则 ``shlex.split`` 后 exec。"""
        # 不要 stdin 的命令读到 EOF：不继承宿主进程的标准输入（那是宿主自己的输入流）
        stdin = asyncio.subprocess.PIPE if spec.stdin else asyncio.subprocess.DEVNULL
        if spec.shell:
            proc = await asyncio.create_subprocess_shell(
                spec.command,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd,
                env=spec.env,
                start_new_session=True,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(spec.command),
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd,
                env=spec.env,
                start_new_session=True,
            )
        return _LocalProcess(proc)


__all__ = [
    "CommandExecutor",
    "CommandInput",
    "CommandOutput",
    "CommandProcess",
    "CommandSpec",
    "LocalCommandExecutor",
    "StreamingCommandProcess",
]
