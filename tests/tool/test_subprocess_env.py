"""Wave 3 复现:内置工具派生子进程的默认 env 白名单。

改动前 ``make_shell_exec_tool`` / ``make_run_in_background_tool`` 的 ``env=None``
直接继承宿主完整 os.environ —— 子进程能读到 API key 等全部凭据。同仓
``ShellScriptExecutor`` 早有 ``_default_safe_env()`` 白名单,两个内置工具没复用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import PermissionPolicy
from taifeng.tool.builtins.background import (
    BackgroundTaskRegistry,
    make_run_in_background_tool,
    make_wait_for_task_tool,
)
from taifeng.tool.builtins.shell import make_shell_exec_tool
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    import pytest

_SECRET = "LLM_BOOTSTRAP_API_KEY"
_DUMP = "/usr/bin/env"  # 绝对路径:显式 env 场景下子进程没有 PATH


def _policy() -> PermissionPolicy:
    """shell_exec 默认拒绝无 policy 调用；本组测的是 env 而非授权。"""
    return PermissionPolicy(default_mode="allow")


def _ctx() -> ToolContext:
    return ToolContext(call_id="c1", cancel=CancellationToken(name="t"), thread_id="t")


async def test_shell_exec_default_env_excludes_host_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 env 必须是白名单:不含宿主凭据,含 PATH。"""
    monkeypatch.setenv(_SECRET, "sk-should-not-leak")
    tool = make_shell_exec_tool(allow_shell=True, policy=_policy())
    result = await tool.handler({"command": _DUMP}, _ctx())

    assert not result.is_error, result.output
    assert "sk-should-not-leak" not in result.output, "宿主凭据泄漏进子进程"
    assert "PATH=" in result.output, "白名单基本变量缺失"


async def test_background_default_env_excludes_host_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_in_background 同规则。"""
    monkeypatch.setenv(_SECRET, "sk-should-not-leak")
    registry = BackgroundTaskRegistry()
    try:
        spawn = make_run_in_background_tool(registry=registry, policy=_policy())
        spawned = await spawn.handler({"command": _DUMP}, _ctx())
        assert not spawned.is_error, spawned.output
        task_id = spawned.data["task_id"]

        waited = await make_wait_for_task_tool(registry=registry).handler(
            {"task_id": task_id, "timeout_seconds": 10}, _ctx(),
        )
        assert "sk-should-not-leak" not in waited.output, "宿主凭据泄漏进后台子进程"
    finally:
        await registry.shutdown()


async def test_explicit_env_still_wins() -> None:
    """显式传 env 时原样生效（白名单不再叠加）。"""
    tool = make_shell_exec_tool(
        allow_shell=True, policy=_policy(), env={"MARKER": "explicit-value"},
    )
    result = await tool.handler({"command": _DUMP}, _ctx())
    assert "MARKER=explicit-value" in result.output


def test_shared_whitelist_is_single_implementation() -> None:
    """白名单实现必须只有一份（工具与 ScriptExecutor 共用）。"""
    from taifeng.skill.scripts import shell as script_shell
    from taifeng.tool.subprocess_env import default_safe_env

    assert script_shell._default_safe_env is default_safe_env  # noqa: SLF001
    env: dict[str, Any] = default_safe_env()
    assert "PATH" in env and _SECRET not in env
