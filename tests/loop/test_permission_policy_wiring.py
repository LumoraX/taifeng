"""EnginePool / AgentEngine 的 permission_policy 注入路径测试（G1 T1）。

覆盖 spec ``permission-gate`` ADDED Requirements:
    - "EnginePool 暴露 permission_policy 注入参数"
    - "AgentEngine 透传 permission_policy / request_metadata 到 TurnRunner"

策略：
    - mock LLM 一轮返回一个 tool call → TurnRunner 走 tool dispatch
    - 用 PreToolUseHook 在 dispatch 时捕获 TurnRunner.permission_policy
    - 断言它就是构造时注入的对象
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import taifeng
from taifeng.hooks.types import HookContext, HookDecision, HookKind
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.permission.types import PermissionPolicy
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec


@dataclass
class _CapturingHookRunner:
    """记录 PreToolUseHook 触发时所见到的 turn_runner.permission_policy。"""
    captured: list[Any] = field(default_factory=list)

    async def run(self, kind: HookKind, hook_data: Any, ctx: HookContext) -> HookDecision:
        # 通过 hook_data 间接接近 turn_runner 不容易；改成直接读 captured marker
        return HookDecision.ok()


async def _spy_tool_handler_factory(captured: list[Any]):
    """构造一个 tool，handler 内通过 ctx.extras 读取 permission_policy。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        captured.append(ctx.extras.get("permission_policy"))
        return ToolResult.ok("captured")

    return ToolSpec(
        name="spy",
        description="capture permission_policy from ctx.extras",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        parallel_safe=True,
    )


@pytest.mark.asyncio
async def test_pool_to_turn_wiring_with_permission_policy(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """pool 注入 permission_policy 后，tool dispatch 时 ctx.extras 含同一对象。"""
    captured: list[Any] = []
    spy_tool = await _spy_tool_handler_factory(captured)

    # composite skill 默认 tool_names 不含 spy；通过 extra_tools 注册全局可见
    # 但 entry_skill.tool_names 不含 'spy' 会被 turn.py 过滤掉。改写 SKILL 让它认。
    skill_md = """---
name: spy-skill
description: spy entry
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: [spy]
max_call_depth: 2
---
# Spy
"""
    (skills_dir / "spy-skill").mkdir()
    (skills_dir / "spy-skill" / "SKILL.md").write_text(skill_md, encoding="utf-8")

    client = MockClient(turns=[
        MockTurn(
            text="calling spy",
            tool_calls=[{
                "id": "tc1",
                "name": "spy",
                "arguments": "{}",
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        ),
        MockTurn(
            text="done",
            usage=TokenUsage(input_tokens=20, output_tokens=5),
        ),
    ])

    my_policy = PermissionPolicy(rules=[], default_mode="allow")
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[spy_tool],
        permission_policy=my_policy,
        request_metadata={"x_corr": "v-x"},
    )
    engine = await pool.get_or_create(
        session_id="wire-test", entry_skill_id="spy-skill",
    )
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    await pool.close()

    assert captured, "spy tool should have been invoked"
    assert captured[0] is my_policy, (
        f"expected the same PermissionPolicy object, got: {captured[0]!r}"
    )


@pytest.mark.asyncio
async def test_pool_without_permission_policy_passes_none(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """未注入 permission_policy 时 ctx.extras['permission_policy'] 是 None（向后兼容）。"""
    captured: list[Any] = []
    spy_tool = await _spy_tool_handler_factory(captured)

    skill_md = """---
name: spy-skill
description: spy entry
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: [spy]
max_call_depth: 2
---
# Spy
"""
    (skills_dir / "spy-skill").mkdir()
    (skills_dir / "spy-skill" / "SKILL.md").write_text(skill_md, encoding="utf-8")

    client = MockClient(turns=[
        MockTurn(
            text="x",
            tool_calls=[{"id": "tc1", "name": "spy", "arguments": "{}"}],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        ),
        MockTurn(text="done", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[spy_tool],
    )
    engine = await pool.get_or_create(
        session_id="no-pol", entry_skill_id="spy-skill",
    )
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    await pool.close()

    assert captured == [None], f"expected [None], got {captured!r}"
