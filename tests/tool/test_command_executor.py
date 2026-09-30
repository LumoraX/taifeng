"""sandbox-seam —— shell_exec / run_in_background 经 CommandExecutor 启动进程 + shell 响应取消。"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

from taifeng.loop.cancellation import CancellationToken, CancelReason
from taifeng.permission import PermissionDecision, PermissionPolicy, PermissionRequest
from taifeng.tool.builtins.background import BackgroundTaskRegistry
from taifeng.tool.builtins.shell import make_shell_exec_tool
from taifeng.tool.command_executor import (
    CommandExecutor,
    CommandSpec,
    LocalCommandExecutor,
)
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


class _AllowAll:
    """放行一切的最小 prompter。"""

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.allow(reason="test")


def _policy() -> PermissionPolicy:
    return PermissionPolicy.from_dict({"allow": ["ShellExec(*)"]}, prompter=_AllowAll())


class _RecordingExecutor:
    """记录收到的 CommandSpec，再交给本机执行器（模拟沙箱包装层）。"""

    def __init__(self) -> None:
        self.specs: list[CommandSpec] = []
        self._inner = LocalCommandExecutor()

    async def start(self, spec: CommandSpec):  # noqa: ANN201
        self.specs.append(spec)
        return await self._inner.start(spec)


def test_local_executor_satisfies_protocol() -> None:
    assert isinstance(LocalCommandExecutor(), CommandExecutor)


async def test_shell_exec_goes_through_injected_executor() -> None:
    """shell_exec 经注入的执行器启动，spec 带白名单 env 与 shell 模式。"""
    executor = _RecordingExecutor()
    tool = make_shell_exec_tool(policy=_policy(), executor=executor)
    result = await tool.handler(
        {"command": "echo sandboxed"}, ToolContext(call_id="c", cancel=CancellationToken(), thread_id="t"))
    assert not result.is_error and "sandboxed" in result.output
    (spec,) = executor.specs
    assert spec.shell is True
    assert "PATH" in spec.env and "OPENAI_API_KEY" not in spec.env


async def test_shell_exec_argv_mode_bad_quoting_is_spawn_error() -> None:
    """argv 模式下引号不配对 → spawn_error（不抛出未处理异常）。"""
    tool = make_shell_exec_tool(policy=_policy(), allow_shell=False)
    result = await tool.handler(
        {"command": "echo 'unterminated"}, ToolContext(call_id="c", cancel=CancellationToken(), thread_id="t"))
    assert result.is_error and "spawn_error" in result.output


async def test_shell_exec_cancel_kills_process_promptly() -> None:
    """turn 取消 → 立即杀子进程并以 cancelled 结果收尾（此前只受超时约束）。"""
    tool = make_shell_exec_tool(policy=_policy(), timeout_seconds=30)
    token = CancellationToken()
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, token.cancel, CancelReason.DEADLINE_EXCEEDED)
    started = loop.time()
    result = await tool.handler(
        {"command": "sleep 10"}, ToolContext(call_id="c", cancel=token, thread_id="t"))
    assert loop.time() - started < 2.0
    assert result.is_error and "cancelled" in result.output
    assert "deadline_exceeded" in result.output


async def test_background_registry_uses_injected_executor() -> None:
    """后台任务同样经注入的执行器启动（shell 模式）。"""
    executor = _RecordingExecutor()
    reg = BackgroundTaskRegistry(executor=executor)
    task_id = await reg.spawn("echo bg")
    result = await reg.wait(task_id, timeout=5.0)
    assert "bg" in result["stdout"]
    assert executor.specs[0].shell is True
    await reg.shutdown()


@pytest.mark.parametrize("error", [OSError("no sandbox"), ValueError("bad argv")])
async def test_executor_start_failure_is_spawn_error(error: Exception) -> None:
    """执行器启动失败 → spawn_error 结果，不抛出。"""

    class _Broken:
        async def start(self, spec: CommandSpec):  # noqa: ANN201
            raise error

    tool = make_shell_exec_tool(policy=_policy(), executor=_Broken())
    result = await tool.handler(
        {"command": "echo x"}, ToolContext(call_id="c", cancel=CancellationToken(), thread_id="t"))
    assert result.is_error and "spawn_error" in result.output


# ---------------------------------------------------------------------------
# 进程组终止（ADR 0108）：shell 派生的子进程随 kill 一起结束
# ---------------------------------------------------------------------------


def _forking_command(pidfile: Path) -> str:
    """一条 shell 必须 fork 的命令：后台起 sleep、把它的 pid 写进文件、再等它。"""
    return f"sleep 30 & echo $! > {pidfile}; wait"


async def _child_pid(pidfile: Path) -> int:
    """等命令把子进程 pid 写出来。"""
    for _ in range(500):
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text().strip())
        await asyncio.sleep(0.01)
    raise AssertionError("command never reported its child pid")


async def _gone(pid: int) -> bool:
    """进程是否已不存在（给内核一点回收时间）。"""
    for _ in range(300):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_shell_exec_cancel_kills_descendants_and_returns_promptly(tmp_path: Path) -> None:
    """取消不等 shell 派生的子进程自己跑完，也不把它留成孤儿。"""
    pidfile = tmp_path / "child.pid"
    tool = make_shell_exec_tool(policy=_policy(), timeout_seconds=60)
    token = CancellationToken()
    loop = asyncio.get_running_loop()

    async def cancel_once_running() -> int:
        pid = await _child_pid(pidfile)
        token.cancel(CancelReason.DEADLINE_EXCEEDED)
        return pid

    watcher = asyncio.create_task(cancel_once_running())
    started = loop.time()
    result = await tool.handler(
        {"command": _forking_command(pidfile)},
        ToolContext(call_id="c", cancel=token, thread_id="t"))
    assert loop.time() - started < 5.0
    assert result.is_error and "cancelled" in result.output
    assert await _gone(await watcher)


async def test_shell_exec_timeout_kills_descendants_and_returns_promptly(tmp_path: Path) -> None:
    """超时同理：杀的是整个进程组。"""
    pidfile = tmp_path / "child.pid"
    tool = make_shell_exec_tool(policy=_policy(), timeout_seconds=1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await tool.handler(
        {"command": _forking_command(pidfile)},
        ToolContext(call_id="c", cancel=CancellationToken(), thread_id="t"))
    assert loop.time() - started < 5.0
    assert result.is_error and "timeout" in result.output
    assert await _gone(await _child_pid(pidfile))


async def test_background_kill_takes_descendants_with_it(tmp_path: Path) -> None:
    """后台任务被 kill 时，它派生的子进程一并结束。"""
    pidfile = tmp_path / "child.pid"
    reg = BackgroundTaskRegistry()
    task_id = await reg.spawn(_forking_command(pidfile))
    pid = await _child_pid(pidfile)
    assert await asyncio.wait_for(reg.kill(task_id), timeout=5.0) is True
    assert await _gone(pid)
    await reg.shutdown()


async def test_background_shutdown_takes_descendants_with_it(tmp_path: Path) -> None:
    """registry 关闭时同样不留孤儿进程。"""
    pidfile = tmp_path / "child.pid"
    reg = BackgroundTaskRegistry()
    await reg.spawn(_forking_command(pidfile))
    pid = await _child_pid(pidfile)
    await asyncio.wait_for(reg.shutdown(), timeout=5.0)
    assert await _gone(pid)


async def test_local_process_kill_after_completion_is_a_noop() -> None:
    """已经收完输出的进程再 kill 不报错、也不会去碰别的进程组。"""
    proc = await LocalCommandExecutor().start(
        CommandSpec(command="echo done", shell=True, cwd=None, env={"PATH": os.defpath}))
    stdout, _ = await proc.communicate()
    assert stdout.strip() == b"done" and proc.returncode == 0
    proc.kill()
    assert await proc.wait() == 0


async def test_local_process_satisfies_protocol_in_both_modes() -> None:
    from taifeng.tool.command_executor import CommandProcess

    for shell, command in ((True, "echo a | cat"), (False, "echo b")):
        proc = await LocalCommandExecutor().start(
            CommandSpec(command=command, shell=shell, cwd=None, env={"PATH": os.defpath}))
        assert isinstance(proc, CommandProcess)
        stdout, stderr = await proc.communicate()
        assert stdout.strip() in (b"a", b"b") and stderr == b"" and proc.returncode == 0
