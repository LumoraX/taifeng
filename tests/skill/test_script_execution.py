"""scripts-runtime 跨模块集成测试。

T1 部分：仅覆盖 ``ScriptDescriptor`` / ``ScriptInvocation`` / ``ScriptResult`` 的
基础契约（构造、校验、不可变性）。Executor 行为测试在 T3/T4/T6 各自文件内。

参见 ``docs/architecture/capabilities/script-execution.md``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng import (
    ScriptDescriptor,
    ScriptExecutionError,
    ScriptExecutor,
    ScriptInvocation,
    ScriptResult,
)
from taifeng.loop import CancellationToken


def test_descriptor_construction() -> None:
    """正常字段构造 + ``full_target`` 计算。"""
    descriptor = ScriptDescriptor(
        skill_id="data-prep",
        name="normalize",
        path=Path("/tmp/normalize.sh"),
        language="shell",
        description="把 CSV 标准化",
        args_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        timeout_seconds=30.0,
        max_output_bytes=4096,
    )
    assert descriptor.full_target == "data-prep/normalize"
    assert descriptor.timeout_seconds == 30.0
    assert descriptor.max_output_bytes == 4096
    # frozen dataclass 不可变
    with pytest.raises((AttributeError, Exception)):
        descriptor.name = "other"  # type: ignore[misc]


def test_descriptor_rejects_zero_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ScriptDescriptor(
            skill_id="x",
            name="y",
            path=Path("/tmp/y.sh"),
            language="shell",
            timeout_seconds=0.0,
        )


def test_descriptor_rejects_zero_output_bytes() -> None:
    with pytest.raises(ValueError, match="max_output_bytes"):
        ScriptDescriptor(
            skill_id="x",
            name="y",
            path=Path("/tmp/y.sh"),
            language="shell",
            max_output_bytes=0,
        )


def test_descriptor_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        ScriptDescriptor(
            skill_id="x",
            name="",
            path=Path("/tmp/y.sh"),
            language="shell",
        )


def test_invocation_binds_cancel_token() -> None:
    descriptor = ScriptDescriptor(
        skill_id="x",
        name="y",
        path=Path("/tmp/y.sh"),
        language="shell",
    )
    cancel = CancellationToken(name="root")
    inv = ScriptInvocation(descriptor=descriptor, args={"input": "a.csv"}, cancel=cancel)
    assert inv.descriptor is descriptor
    assert inv.args == {"input": "a.csv"}
    assert inv.cancel is cancel


def test_script_result_ok_property() -> None:
    """``ok`` 仅在 ``exit_code == 0`` 且既未超时也未被 kill 时为真。"""
    success = ScriptResult(exit_code=0, stdout="ok", stderr="", duration_ms=10)
    assert success.ok

    failed_exit = ScriptResult(exit_code=1, stdout="", stderr="boom", duration_ms=10)
    assert not failed_exit.ok

    timeout = ScriptResult(
        exit_code=-15, stdout="", stderr="", duration_ms=100, is_timeout=True, killed=True
    )
    assert not timeout.ok

    killed = ScriptResult(
        exit_code=-9, stdout="", stderr="", duration_ms=50, killed=True
    )
    assert not killed.ok


def test_executor_protocol_is_runtime_checkable() -> None:
    """``ScriptExecutor`` 是 ``runtime_checkable`` Protocol，可用 ``isinstance``。"""

    class FakeExecutor:
        async def execute(self, inv: ScriptInvocation) -> ScriptResult:
            return ScriptResult(exit_code=0, stdout="", stderr="", duration_ms=0)

    assert isinstance(FakeExecutor(), ScriptExecutor)

    class NotAnExecutor:
        pass

    assert not isinstance(NotAnExecutor(), ScriptExecutor)


def test_script_execution_error_carries_descriptor() -> None:
    descriptor = ScriptDescriptor(
        skill_id="x",
        name="y",
        path=Path("/tmp/y.sh"),
        language="shell",
    )
    cause = OSError("boom")
    err = ScriptExecutionError("init failed", descriptor=descriptor, cause=cause)
    assert err.descriptor is descriptor
    assert err.cause is cause
    assert err.kind == "script_execution_failed"
    assert err.retryable is False


# ============================================================
# T6 集成场景 —— 跨 executor / tool / loader 联动
# ============================================================


import asyncio  # noqa: E402

from taifeng.hooks.types import HookDecision, HookRegistry, HookRunner  # noqa: E402
from taifeng.permission.types import (  # noqa: E402
    PermissionPolicy,
)
from taifeng.skill.definition import SkillDefinition  # noqa: E402
from taifeng.skill.registry import SkillSnapshot  # noqa: E402
from taifeng.skill.scripts.shell import ShellScriptExecutor  # noqa: E402
from taifeng.tool.builtins.run_script import make_run_script_tool  # noqa: E402
from taifeng.tool.spec import ToolContext  # noqa: E402


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _build_skill_with_script(
    skill_id: str, script: ScriptDescriptor
) -> SkillDefinition:
    return SkillDefinition(
        id=skill_id,
        name=skill_id,
        description="t",
        version="1.0.0",
        body="b",
        body_path=Path("/tmp/SKILL.md"),
        type="composite",
        entry=True,
        scripts=(script,),
        child_skills=frozenset(),
        tool_names=frozenset(),
    )


def _make_ctx(skill: SkillDefinition, extras_override: dict | None = None) -> ToolContext:
    extras = {
        "skill_snapshot": SkillSnapshot(version=1, skills=(skill,)),
        "script_executors": {"shell": ShellScriptExecutor()},
        "current_skill": skill,
        "submission_id": "sub-1",
        "entry_skill_id": skill.id,
        "turn_index": 1,
    }
    extras.update(extras_override or {})
    return ToolContext(
        call_id="call-1",
        cancel=CancellationToken(name="test"),
        thread_id="thr-1",
        extras=extras,
    )


async def test_subprocess_killed_on_cancel_e2e(tmp_path: Path) -> None:
    """R4 红线：cancel 必须立刻 kill subprocess。"""
    script = tmp_path / "sleep.sh"
    _write_executable(script, "#!/bin/sh\nsleep 60\n")
    descriptor = ScriptDescriptor(
        skill_id="s",
        name="sl",
        path=script,
        language="shell",
        timeout_seconds=300.0,
    )
    cancel = CancellationToken()
    inv = ScriptInvocation(descriptor=descriptor, args={}, cancel=cancel)

    async def cancel_soon() -> None:
        await asyncio.sleep(0.3)
        cancel.cancel()

    import time
    start = time.monotonic()
    _, result = await asyncio.gather(
        cancel_soon(), ShellScriptExecutor().execute(inv)
    )
    assert result.killed is True
    assert time.monotonic() - start < 2.0


async def test_argv_no_shell_injection_in_python(tmp_path: Path) -> None:
    """PythonScriptExecutor 同样不 shell-expand args。"""
    from taifeng.skill.scripts.python import PythonScriptExecutor

    script = tmp_path / "echo.py"
    _write_executable(script, "import sys\nprint(sys.argv[1:])\n")
    descriptor = ScriptDescriptor(
        skill_id="s",
        name="echo",
        path=script,
        language="python",
        timeout_seconds=5.0,
        args_schema={
            "type": "object",
            "properties": {"p": {"type": "string"}},
        },
    )
    inv = ScriptInvocation(
        descriptor=descriptor,
        args={"p": "; cat /etc/passwd"},
        cancel=CancellationToken(),
    )
    result = await PythonScriptExecutor().execute(inv)
    assert result.exit_code == 0
    assert "; cat /etc/passwd" in result.stdout
    # /etc/passwd 内容片段（如 root:x:0）SHALL NOT 出现
    assert "root:x:" not in result.stdout


async def test_pre_hook_args_override(tmp_path: Path) -> None:
    """pre_script_use hook 通过 metadata['args_override'] 替换 args。"""
    script = tmp_path / "showarg.sh"
    _write_executable(script, '#!/bin/sh\necho got=$1\n')
    descriptor = ScriptDescriptor(
        skill_id="s",
        name="showarg",
        path=script,
        language="shell",
        timeout_seconds=5.0,
        args_schema={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    skill = _build_skill_with_script("s", descriptor)

    registry = HookRegistry()

    async def pre(_h, _ctx) -> HookDecision:
        return HookDecision(allow=True, metadata={"args_override": {"x": "OVERRIDDEN"}})

    registry.register("pre_script_use", pre)
    runner = HookRunner(registry)

    tool = make_run_script_tool()
    ctx = _make_ctx(skill, {"hook_runner": runner})
    result = await tool.handler(
        {"skill_id": "s", "script_name": "showarg", "args": {"x": "ORIG"}},
        ctx,
    )
    assert result.is_error is False
    assert "OVERRIDDEN" in result.output
    assert "ORIG" not in result.output


async def test_post_hook_receives_truncated_preview(tmp_path: Path) -> None:
    """post_script_use hook 收到的 stdout_preview ≤ SCRIPT_OUTPUT_PREVIEW_LIMIT。"""
    from taifeng.hooks.types import SCRIPT_OUTPUT_PREVIEW_LIMIT

    script = tmp_path / "long.sh"
    _write_executable(script, "#!/bin/sh\nprintf 'A%.0s' $(seq 1 3000)\n")
    descriptor = ScriptDescriptor(
        skill_id="s",
        name="long",
        path=script,
        language="shell",
        timeout_seconds=5.0,
        max_output_bytes=4096,
    )
    skill = _build_skill_with_script("s", descriptor)

    registry = HookRegistry()
    captured: list = []

    async def post(hook_data, _ctx) -> HookDecision:
        captured.append(hook_data)
        return HookDecision.ok()

    registry.register("post_script_use", post)
    runner = HookRunner(registry)
    tool = make_run_script_tool()
    ctx = _make_ctx(skill, {"hook_runner": runner})
    result = await tool.handler(
        {"skill_id": "s", "script_name": "long", "args": {}}, ctx
    )
    assert result.is_error is False
    assert len(captured) == 1
    assert (
        len(captured[0].stdout_preview.encode("utf-8")) <= SCRIPT_OUTPUT_PREVIEW_LIMIT
    )


async def test_permission_policy_with_chain_in_request(tmp_path: Path) -> None:
    """PermissionPolicy.check 收到的 PermissionRequest 含 scope/target/call_chain。"""
    script = tmp_path / "x.sh"
    _write_executable(script, "#!/bin/sh\necho ok\n")
    descriptor = ScriptDescriptor(
        skill_id="s",
        name="x",
        path=script,
        language="shell",
        timeout_seconds=5.0,
    )
    skill = _build_skill_with_script("s", descriptor)

    captured = []

    async def cb(req):
        captured.append(req)
        from taifeng.permission.types import PermissionDecision
        return PermissionDecision.allow()

    from taifeng.permission.types import CallbackPrompter
    policy = PermissionPolicy(
        rules=[],
        default_mode="ask",
        prompter=CallbackPrompter(cb),
    )

    tool = make_run_script_tool()
    ctx = _make_ctx(skill, {"permission_policy": policy})
    result = await tool.handler(
        {"skill_id": "s", "script_name": "x", "args": {}}, ctx
    )
    assert result.is_error is False
    assert len(captured) == 1
    req = captured[0]
    assert req.scope == "script_exec"
    assert req.target == "s/x"
    assert req.entry_skill_id == "s"
    assert req.thread_id == "thr-1"
    assert req.turn_index == 1
    # call_chain 默认从 caller skill id 启用一个
    assert req.call_chain[-1] == "s"


async def test_post_hook_runs_on_failed_script(tmp_path: Path) -> None:
    """exit_code != 0 时 post hook 仍触发（审计闭环）。"""
    script = tmp_path / "fail.sh"
    _write_executable(script, "#!/bin/sh\necho bye >&2\nexit 9\n")
    descriptor = ScriptDescriptor(
        skill_id="s",
        name="f",
        path=script,
        language="shell",
        timeout_seconds=5.0,
    )
    skill = _build_skill_with_script("s", descriptor)

    registry = HookRegistry()
    saw = []

    async def post(hook_data, _ctx) -> HookDecision:
        saw.append(hook_data.exit_code)
        return HookDecision.ok()

    registry.register("post_script_use", post)
    runner = HookRunner(registry)
    tool = make_run_script_tool()
    ctx = _make_ctx(skill, {"hook_runner": runner})
    result = await tool.handler(
        {"skill_id": "s", "script_name": "f", "args": {}}, ctx
    )
    assert result.is_error is True
    assert saw == [9]
