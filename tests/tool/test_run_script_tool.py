"""T5 — run_script 工具的 9 阶段流程契约测试。

覆盖：
1. 成功路径：执行 ScriptResult.ok → ToolResult.ok
2. unknown_script：descriptor 不在 skill.scripts
3. invalid_args：required 字段缺失
4. permission deny：PermissionPolicy 返回 deny → 不执行 executor
5. pre_script_use hook deny：返回 ToolResult.error("hook_denied")
6. timeout：result.is_timeout → ToolResult.error 含 timeout 标记
7. no_executor_for_language：script 是 js 但没注入 custom executor
8. post hook 仅审计：hook 异常不影响 ToolResult
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from taifeng.hooks.types import HookDecision, HookRegistry, HookRunner
from taifeng.loop import CancellationToken
from taifeng.permission.types import PermissionPolicy, PermissionRule
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.registry import SkillSnapshot
from taifeng.skill.scripts.shell import ShellScriptExecutor
from taifeng.skill.scripts.types import (
    ScriptDescriptor,
    ScriptInvocation,
    ScriptResult,
)
from taifeng.tool.builtins.run_script import make_run_script_tool
from taifeng.tool.spec import ToolContext


def _write_script(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _build_skill(skill_id: str, scripts: tuple[ScriptDescriptor, ...]) -> SkillDefinition:
    return SkillDefinition(
        id=skill_id,
        name=skill_id,
        description="test",
        version="1.0.0",
        body="body",
        body_path=Path("/tmp/SKILL.md"),
        type="composite",
        entry=True,
        child_skills=frozenset(),
        tool_names=frozenset(),
        scripts=scripts,
    )


def _make_snapshot(skill: SkillDefinition) -> SkillSnapshot:
    return SkillSnapshot(version=1, skills=(skill,))


def _make_ctx(
    *,
    snapshot: SkillSnapshot,
    executors: dict,
    permission_policy: PermissionPolicy | None = None,
    hook_runner: HookRunner | None = None,
    current_skill: SkillDefinition | None = None,
) -> ToolContext:
    return ToolContext(
        call_id="call-1",
        cancel=CancellationToken(name="test"),
        thread_id="thr-1",
        extras={
            "skill_snapshot": snapshot,
            "script_executors": executors,
            "permission_policy": permission_policy,
            "hook_runner": hook_runner,
            "current_skill": current_skill,
            "submission_id": "sub-1",
            "entry_skill_id": current_skill.id if current_skill else "",
            "turn_index": 1,
        },
    )


async def test_run_script_success_path(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "hello.sh"
    _write_script(script, "#!/bin/sh\necho hi-from-script\n")
    descriptor = ScriptDescriptor(
        skill_id="skill-a",
        name="hello",
        path=script,
        language="shell",
        timeout_seconds=5.0,
    )
    skill = _build_skill("skill-a", (descriptor,))
    tool = make_run_script_tool()
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": ShellScriptExecutor()},
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "skill-a", "script_name": "hello", "args": {}}, ctx
    )
    assert result.is_error is False
    assert "hi-from-script" in result.output
    assert result.data["exit_code"] == 0
    assert "duration_ms" in result.data


async def test_run_script_unknown_skill(tmp_path: Path) -> None:
    skill = _build_skill("skill-a", ())
    tool = make_run_script_tool()
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": ShellScriptExecutor()},
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "no-such", "script_name": "x", "args": {}}, ctx
    )
    assert result.is_error is True
    assert result.data["reason"] == "unknown_skill"


async def test_run_script_unknown_script(tmp_path: Path) -> None:
    skill = _build_skill("skill-a", ())
    tool = make_run_script_tool()
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": ShellScriptExecutor()},
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "skill-a", "script_name": "missing", "args": {}}, ctx
    )
    assert result.is_error is True
    assert result.data["reason"] == "unknown_script"


async def test_run_script_invalid_args(tmp_path: Path) -> None:
    """args_schema.required 缺失字段 → invalid_args。"""
    script = tmp_path / "scripts" / "needs_arg.sh"
    _write_script(script, "#!/bin/sh\necho $1\n")
    descriptor = ScriptDescriptor(
        skill_id="skill-a",
        name="needs_arg",
        path=script,
        language="shell",
        timeout_seconds=5.0,
        args_schema={
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
    )
    skill = _build_skill("skill-a", (descriptor,))
    tool = make_run_script_tool()
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": ShellScriptExecutor()},
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "skill-a", "script_name": "needs_arg", "args": {}}, ctx
    )
    assert result.is_error is True
    assert result.data["reason"] == "invalid_args"


async def test_run_script_permission_deny_skips_execute(tmp_path: Path) -> None:
    """PermissionPolicy deny → executor 不被调用，post hook 也不触发。"""
    script = tmp_path / "scripts" / "should_not_run.sh"
    _write_script(script, "#!/bin/sh\ntouch /tmp/should-never-exist-{}.marker\n")
    descriptor = ScriptDescriptor(
        skill_id="skill-a",
        name="should_not_run",
        path=script,
        language="shell",
        timeout_seconds=5.0,
    )
    skill = _build_skill("skill-a", (descriptor,))

    calls: list[Any] = []

    class SpyExecutor:
        async def execute(self, inv: ScriptInvocation) -> ScriptResult:
            calls.append(inv)
            return ScriptResult(exit_code=0, stdout="", stderr="", duration_ms=0)

    policy = PermissionPolicy(
        rules=[
            PermissionRule(
                scope="script_exec",
                target_pattern="skill-a/should_not_run",
                mode="deny",
                reason="test_deny",
            )
        ],
        default_mode="allow",
    )

    registry = HookRegistry()
    post_called = False

    async def post_hook(_h: Any, _ctx: Any) -> HookDecision:
        nonlocal post_called
        post_called = True
        return HookDecision.ok()

    registry.register("post_script_use", post_hook)
    runner = HookRunner(registry)

    tool = make_run_script_tool()
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": SpyExecutor()},
        permission_policy=policy,
        hook_runner=runner,
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "skill-a", "script_name": "should_not_run", "args": {}}, ctx
    )
    assert result.is_error is True
    assert result.data["reason"] == "permission_denied"
    assert calls == [], "executor SHALL NOT be called when permission denied"
    assert post_called is False, "post hook SHALL NOT fire when permission denied"


async def test_run_script_pre_hook_deny(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "x.sh"
    _write_script(script, "#!/bin/sh\necho run\n")
    descriptor = ScriptDescriptor(
        skill_id="skill-a", name="x", path=script, language="shell", timeout_seconds=5.0
    )
    skill = _build_skill("skill-a", (descriptor,))

    registry = HookRegistry()

    async def pre(_h: Any, _ctx: Any) -> HookDecision:
        return HookDecision.deny("free-tier blocked")

    registry.register("pre_script_use", pre)
    runner = HookRunner(registry)

    tool = make_run_script_tool()
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": ShellScriptExecutor()},
        hook_runner=runner,
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "skill-a", "script_name": "x", "args": {}}, ctx
    )
    assert result.is_error is True
    assert result.data["reason"] == "hook_denied"
    assert "free-tier" in result.output


async def test_run_script_timeout_marks_error(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "loop.sh"
    _write_script(script, "#!/bin/sh\nwhile :; do sleep 0.1; done\n")
    descriptor = ScriptDescriptor(
        skill_id="skill-a",
        name="loop",
        path=script,
        language="shell",
        timeout_seconds=1.0,
    )
    skill = _build_skill("skill-a", (descriptor,))
    tool = make_run_script_tool()
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": ShellScriptExecutor()},
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "skill-a", "script_name": "loop", "args": {}}, ctx
    )
    assert result.is_error is True
    assert result.data["reason"] == "timeout"
    assert result.data["is_timeout"] is True
    assert result.data["killed"] is True


async def test_run_script_no_executor_for_language(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "x.js"
    _write_script(script, "console.log('hi')\n")
    descriptor = ScriptDescriptor(
        skill_id="skill-a",
        name="x",
        path=script,
        language="custom",
        timeout_seconds=5.0,
    )
    skill = _build_skill("skill-a", (descriptor,))
    tool = make_run_script_tool()
    # 只注入 shell；custom 缺失
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": ShellScriptExecutor()},
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "skill-a", "script_name": "x", "args": {}}, ctx
    )
    assert result.is_error is True
    assert result.data["reason"] == "no_executor_for_language"


async def test_run_script_post_hook_audit_only(tmp_path: Path) -> None:
    """post_script_use hook 异常 / deny 不影响 ToolResult。"""
    script = tmp_path / "scripts" / "ok.sh"
    _write_script(script, "#!/bin/sh\necho fine\n")
    descriptor = ScriptDescriptor(
        skill_id="skill-a",
        name="ok",
        path=script,
        language="shell",
        timeout_seconds=5.0,
    )
    skill = _build_skill("skill-a", (descriptor,))

    registry = HookRegistry()
    saw_post = False

    async def post(_h: Any, _ctx: Any) -> HookDecision:
        nonlocal saw_post
        saw_post = True
        raise RuntimeError("audit hook raised")

    registry.register("post_script_use", post)
    runner = HookRunner(registry)

    tool = make_run_script_tool()
    ctx = _make_ctx(
        snapshot=_make_snapshot(skill),
        executors={"shell": ShellScriptExecutor()},
        hook_runner=runner,
        current_skill=skill,
    )
    result = await tool.handler(
        {"skill_id": "skill-a", "script_name": "ok", "args": {}}, ctx
    )
    assert saw_post is True
    assert result.is_error is False
    assert "fine" in result.output
