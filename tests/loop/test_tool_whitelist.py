"""工具白名单一致性（tool-whitelist 契约）测试。

覆盖三层：
- 声明层：``SkillDefinition.visible_tool_names()`` 单一真相（scripts 自动并入 run_script）
  + composite 空壳校验放宽到「child_skills / tool_names / scripts 至少其一」；
- 可见层：scripts skill 作 entry → sim ledger 断言请求 tools 真含 run_script；
- 可执行层：``dispatch_batch`` 对本轮未提供的工具拒绝执行（is_error 核销、
  hook / runtime 零触达、ToolCallCompleted 照常可观测）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch
from taifeng.skill.definition import SkillDefinition, SkillValidationError
from taifeng.skill.scripts.types import ScriptDescriptor
from taifeng.tool.spec import ToolContext, ToolResult


def _skill(
    *,
    type_: str = "atomic",
    tool_names: frozenset[str] = frozenset(),
    child_skills: frozenset[str] = frozenset(),
    with_script: bool = False,
    entry: bool = False,
) -> SkillDefinition:
    """构造最小 SkillDefinition（scripts 可选）。"""
    scripts = (
        ScriptDescriptor(
            skill_id="s1", name="probe", path=Path("scripts/probe.py"), language="python"
        ),
    ) if with_script else ()
    return SkillDefinition(
        id="s1", name="S1", description="d", version="1.0.0",
        body="b", body_path=Path("SKILL.md"),
        type=type_, entry=entry,
        tool_names=tool_names, child_skills=child_skills, scripts=scripts,
    )


# ── 声明层：visible_tool_names 单一真相 ─────────────────────────────────────


def test_atomic_with_scripts_sees_run_script() -> None:
    """atomic + scripts（不声明也不允许声明 tool_names）→ run_script 自动可见。"""
    sk = _skill(with_script=True)
    sk.validate()  # atomic + scripts 合法
    assert sk.visible_tool_names() == frozenset({"read_skill", "call_skill", "run_script"})


def test_no_scripts_no_run_script() -> None:
    """未声明 scripts → 可见集不含 run_script（只有内核恒备二件套）。"""
    sk = _skill()
    assert sk.visible_tool_names() == frozenset({"read_skill", "call_skill"})


def test_composite_union_of_declared_and_auto() -> None:
    """composite：tool_names 显式声明 ∪ 内核恒备 ∪ scripts 自动并入。"""
    sk = _skill(type_="composite", tool_names=frozenset({"file_read"}), with_script=True)
    sk.validate()
    assert sk.visible_tool_names() == frozenset(
        {"file_read", "read_skill", "call_skill", "run_script"}
    )


def test_strict_tool_names_hides_builtin_skill_tools() -> None:
    """strict_tool_names=True：只暴露声明工具与脚本工具，隐藏内核 skill 工具。"""
    sk = _skill(type_="composite", tool_names=frozenset({"spawn_skill"}))
    sk = SkillDefinition(
        id=sk.id,
        name=sk.name,
        description=sk.description,
        version=sk.version,
        body=sk.body,
        body_path=sk.body_path,
        type=sk.type,
        entry=sk.entry,
        child_skills=sk.child_skills,
        tool_names=sk.tool_names,
        max_call_depth=sk.max_call_depth,
        frontmatter_raw={"strict_tool_names": True},
    )

    assert sk.visible_tool_names() == frozenset({"spawn_skill"})


def test_scripts_only_composite_is_legal() -> None:
    """空壳校验放宽：scripts-only composite 有 agency，合法。"""
    _skill(type_="composite", with_script=True).validate()
    with pytest.raises(SkillValidationError, match="至少声明"):
        _skill(type_="composite").validate()  # 三者全空仍是空壳


# ── 可见层：scripts skill 的请求 tools 真含 run_script（sim 集成）────────────


async def test_entry_with_scripts_offers_run_script_in_request(tmp_path: Path) -> None:
    """scripts-only composite 作 entry → sim ledger 断言请求 tools 含 run_script。"""
    skill_dir = tmp_path / "skills" / "script-runner"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "probe.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        """---
id: script-runner
name: Script Runner
description: 脚本探针
version: 1.0.0
type: composite
entry: true
scripts:
  - name: probe
    path: scripts/probe.py
    language: python
    timeout_seconds: 3
    description: 探针脚本
---
跑 probe 脚本。
""",
        encoding="utf-8",
    )
    client = SimClient(turns=[SimTurn(text="完成")])
    pool = await taifeng.EnginePool.create(
        skills_dir=tmp_path / "skills", threads_dir=tmp_path / "threads",
        model_client=client, compressors=[],
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="script-runner")
        sub = await engine.submit(taifeng.UserMessage(text="开始"))
        async for ev in engine.subscribe(sub):
            if ev.msg.kind in ("turn_completed", "turn_failed"):
                assert ev.msg.kind == "turn_completed"
                break
        # 声明即可见闭环：scripts 自动并入，LLM 真实看得到 run_script
        assert "run_script" in client.ledger.tool_names()
    finally:
        await pool.close()


# ── 可执行层：dispatch_batch 的 not_offered 拒绝 ────────────────────────────


class _RecordingRuntime:
    """记录 dispatch 是否被触达的 fake runtime。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch(self, *, name: str, arguments: dict, ctx: Any) -> ToolResult:
        self.calls.append(name)
        return ToolResult(output=f"out:{name}", is_error=False)


def _ctx(call_id: str) -> ToolContext:
    return ToolContext(call_id=call_id, cancel=CancellationToken(), thread_id="t1")


def _req(name: str) -> ToolCallRequest:
    return ToolCallRequest(
        index=0, call_id="c0", name=name, arguments={},
        arguments_raw="{}", parallel_safe=True,
    )


async def _dispatch(name: str, visible: frozenset[str], hooks: Any = None):
    """跑单条 batch 分发，返回 (outcome, emitted_events)。"""
    events: list[Any] = []

    async def emit(msg: Any) -> None:
        events.append(msg)

    runtime = _RecordingRuntime()
    outcomes = await dispatch_batch(
        [_req(name)], runtime=runtime, ctx_for=_ctx, hooks=hooks, emit=emit,
        semaphore=asyncio.Semaphore(1),
        thread_id="t1", submission_id="s1", entry_skill_id="e1",
        visible_tools=visible,
    )
    return outcomes[0], events, runtime


async def test_not_offered_tool_rejected_with_error_output() -> None:
    """registry 有但本轮未提供 → is_error 核销（reason=not_offered），runtime 零触达。"""
    outcome, events, runtime = await _dispatch("sneaky", frozenset({"file_read"}))
    assert outcome.result.is_error
    assert "tool_not_offered: sneaky" in outcome.result.output
    assert runtime.calls == []  # 未执行
    # R3：拒绝照常 emit ToolCallCompleted（is_error=True），call_id 可核销
    assert [e.kind for e in events] == ["tool_call_completed"]
    assert events[0].data["is_error"] is True
    assert events[0].data["call_id"] == "c0"


async def test_not_offered_does_not_consume_hooks() -> None:
    """幻觉调用不消耗 hook：PreToolUse 不被触发。"""
    from taifeng.hooks.types import HookDecision, HookRegistry, HookRunner

    seen: list[str] = []

    async def pre_hook(hook: Any, ctx: Any) -> HookDecision:
        seen.append(hook.tool_name)
        return HookDecision.ok()

    registry = HookRegistry()
    registry.register("pre_tool_use", pre_hook)
    runner = HookRunner(registry)
    outcome, _, _ = await _dispatch("sneaky", frozenset(), hooks=runner)
    assert outcome.result.is_error
    assert seen == []  # hook 之前已拒


async def test_offered_tool_passes_through() -> None:
    """本轮已提供的工具：校验放行，链路行为与变更前一致。"""
    outcome, events, runtime = await _dispatch("file_read", frozenset({"file_read"}))
    assert not outcome.result.is_error
    assert outcome.result.output == "out:file_read"
    assert runtime.calls == ["file_read"]
