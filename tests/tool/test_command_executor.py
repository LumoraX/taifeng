"""sandbox-seam —— shell_exec / run_in_background 经 CommandExecutor 启动进程 + shell 响应取消。"""

from __future__ import annotations

import asyncio

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
