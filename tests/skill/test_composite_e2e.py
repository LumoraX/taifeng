"""Composite skill 多层 E2E 测试 —— 通过 EnginePool + MockClient 跑通三层派发。

覆盖 spec `skill-dispatch` 在真实 Engine 路径上的端到端行为：
    - 三层 entry → middle → leaf 完整派发 + 返回；SkillDispatched 事件嵌套关系
    - DispatchPolicy.max_call_depth 在第二层 sub-skill 调用时触发拦截
    - 运行时环检测：child 试图回调 entry → cycle_detected
    - submission_id 在跨层派发中保持一致（envelope 级别）

参照：tests/loop/test_engine_e2e.py::test_engine_run_script_e2e 的 EnginePool 模式。

注：``engine.subscribe(sub_id)`` 在第一次 ``turn_completed`` 即退出 —— 但子
skill 的 turn_completed 比 entry 早 emit，会让事件流早退。本文件改用
``engine.subscribe_all()`` + 深度计数器，仅在「outermost turn 完成」时退出。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage

if TYPE_CHECKING:
    from pathlib import Path


# ====================================================================
# fixtures —— 构造三层 skill 目录
# ====================================================================

_ENTRY_BODY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [middle]
tool_names: []
max_call_depth: {max_depth}
---
# entry body
"""

_MIDDLE_BODY = """---
name: middle
description: 中间层
version: 1.0.0
type: composite
model: mock-model
child_skills: [leaf]
tool_names: []
max_call_depth: {max_depth}
---
# middle body
"""

_LEAF_BODY = """---
name: leaf
description: 叶子层
version: 1.0.0
type: atomic
---
# leaf body
"""


def _build_skills(tmp_path: Path, *, max_depth: int = 6) -> Path:
    """构造 entry / middle / leaf 三层 skill；max_depth 控制 depth 上限。

    middle.child_skills 同时含 [leaf, entry] —— 既支持下钻 leaf，也允许尝试
    回调 entry 触发 runtime cycle 检测（静态白名单不拦，cycle 由 runtime 抓）。
    """
    skills = tmp_path / "skills"

    for sid, body_tpl in (
        ("entry", _ENTRY_BODY),
        ("middle", _MIDDLE_BODY),
    ):
        d = skills / sid
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            body_tpl.format(max_depth=max_depth), encoding="utf-8",
        )

    leaf_dir = skills / "leaf"
    leaf_dir.mkdir(parents=True)
    (leaf_dir / "SKILL.md").write_text(_LEAF_BODY, encoding="utf-8")

    return skills


async def _run_until_outer_done(
    engine: taifeng.AgentEngine,
    message: taifeng.UserMessage,
    *,
    deadline_seconds: float = 10.0,
) -> tuple[str, list]:
    """提交 message；用 subscribe_all 收集事件直到「outermost turn 完成」。

    子 skill 的 turn_completed 与 entry 的同一种事件类型，本函数通过
    skill_dispatched / skill_returned 计数推断当前嵌套深度，仅在 depth==0
    且 turn_completed/turn_failed 时退出。

    返回 (sub_id, events) —— events 已按 sub_id 过滤。
    """
    sub_id_holder: list[str] = []
    events: list = []
    done = asyncio.Event()

    async def collector() -> None:
        depth = 0
        async for ev in engine.subscribe_all():
            if not sub_id_holder or ev.submission_id != sub_id_holder[0]:
                continue
            events.append(ev)
            if ev.msg.kind == "skill_dispatched":
                depth += 1
            elif ev.msg.kind == "skill_returned":
                depth -= 1
            if (
                ev.msg.kind in ("turn_completed", "turn_failed")
                and depth == 0
            ):
                done.set()
                return

    task = asyncio.create_task(collector())
    # 让 collector 先注册 subscribe_all 队列
    await asyncio.sleep(0)
    sub_id = await engine.submit(message)
    sub_id_holder.append(sub_id)
    try:
        await asyncio.wait_for(done.wait(), timeout=deadline_seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return sub_id, events


def _kinds(events: list) -> list[str]:
    return [ev.msg.kind for ev in events]


# ====================================================================
# 1. 三层完整派发：entry → middle → leaf → 回流
# ====================================================================

@pytest.mark.asyncio
async def test_three_level_dispatch_emits_correct_stack(
    tmp_path: Path,
) -> None:
    """entry → middle → leaf 完整跑通；SkillDispatched 事件携带正确的 stack_path。

    Mock 5 个 turn：
        1. entry turn 1: call_skill(middle)
        2. middle turn 1: call_skill(leaf)
        3. leaf turn 1: final text（无 tool call）
        4. middle turn 2: 最终文本回流给 entry
        5. entry turn 2: 最终文本给用户
    """
    skills = _build_skills(tmp_path)
    threads = tmp_path / "threads"
    threads.mkdir()

    client = MockClient(turns=[
        # entry turn 1 → call middle
        MockTurn(
            text="派发到 middle",
            tool_calls=[{
                "id": "tc_mid",
                "name": "call_skill",
                "arguments": '{"skill_id": "middle", "args": {"q": "x"}}',
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        # middle turn 1 → call leaf
        MockTurn(
            text="派发到 leaf",
            tool_calls=[{
                "id": "tc_leaf",
                "name": "call_skill",
                "arguments": '{"skill_id": "leaf", "args": {"q": "y"}}',
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        # leaf turn 1 → 终结
        MockTurn(
            text="leaf 输出：完成",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        # middle turn 2 → 终结
        MockTurn(
            text="middle 综合：done",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        # entry turn 2 → 终结
        MockTurn(
            text="entry 总结：所有子任务完成",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads, model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s-3lvl", entry_skill_id="entry",
    )

    sub_id, events = await _run_until_outer_done(
        engine, taifeng.UserMessage(text="请下钻三层"),
    )
    await pool.close()

    # 最后一个事件必须是 outermost turn_completed
    assert events[-1].msg.kind == "turn_completed", _kinds(events)

    # is_root 字段：子 turn 的 turn_completed is_root=False，
    # 只有最外层 (entry) 的 turn_completed is_root=True。3 层下钻 → 3 个
    # turn_completed，其中末尾那个（也是事件流最后一项）必须是 is_root=True，
    # 前两个 (leaf, middle) 必须 is_root=False。
    turn_completeds = [ev for ev in events if ev.msg.kind == "turn_completed"]
    assert len(turn_completeds) == 3, _kinds(events)
    assert turn_completeds[-1].msg.data.get("is_root") is True, (
        f"outermost turn_completed.is_root != True: {turn_completeds[-1].msg.data}"
    )
    for sub_tc in turn_completeds[:-1]:
        assert sub_tc.msg.data.get("is_root") is False, (
            f"sub turn_completed.is_root != False: {sub_tc.msg.data}"
        )

    # 提取所有 SkillDispatched 事件
    dispatched = [
        ev for ev in events if ev.msg.kind == "skill_dispatched"
    ]
    # 期望 2 次派发：entry→middle、middle→leaf
    assert len(dispatched) == 2, [d.msg.data for d in dispatched]

    # 第一次：派发 middle，stack_path = [entry, middle]
    d0 = dispatched[0].msg.data
    assert d0["skill_id"] == "middle"
    assert d0["stack_path"] == ["entry", "middle"]

    # 第二次：派发 leaf，stack_path = [entry, middle, leaf]
    d1 = dispatched[1].msg.data
    assert d1["skill_id"] == "leaf"
    assert d1["stack_path"] == ["entry", "middle", "leaf"]
    # 深度递增（叶子比中间深 1）
    assert d1["depth"] > d0["depth"]

    # 同样应有 2 次 SkillReturned，与派发对称
    returned = [ev for ev in events if ev.msg.kind == "skill_returned"]
    assert len(returned) == 2, _kinds(events)

    # 所有事件 submission_id 必须一致（envelope 级别）
    sub_ids = {ev.submission_id for ev in events}
    assert sub_ids == {sub_id}


# ====================================================================
# 2. max_depth 在子 skill 调用时触发拦截
# ====================================================================

@pytest.mark.asyncio
async def test_max_depth_blocks_grandchild_dispatch(
    tmp_path: Path,
) -> None:
    """max_call_depth=2 时，entry→middle OK，middle→leaf 被 DispatchPolicy 拦截。

    Mock 4 个 turn：
        1. entry: call_skill(middle)
        2. middle: call_skill(leaf) —— 应被拒
        3. middle turn 2: 看到 error 后给终结文本
        4. entry turn 2: 终结
    """
    skills = _build_skills(tmp_path, max_depth=2)
    threads = tmp_path / "threads"
    threads.mkdir()

    client = MockClient(turns=[
        MockTurn(
            text="派发 middle",
            tool_calls=[{
                "id": "tc_mid",
                "name": "call_skill",
                "arguments": '{"skill_id": "middle", "args": {}}',
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        MockTurn(
            text="尝试派发 leaf",
            tool_calls=[{
                "id": "tc_leaf_blocked",
                "name": "call_skill",
                "arguments": '{"skill_id": "leaf", "args": {}}',
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        MockTurn(
            text="收到深度限制错误，中止下钻",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        MockTurn(
            text="entry 终结",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads, model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s-depth", entry_skill_id="entry",
    )
    sub_id, events = await _run_until_outer_done(
        engine, taifeng.UserMessage(text="depth test"),
    )
    await pool.close()

    assert events[-1].msg.kind == "turn_completed", _kinds(events)

    # 只应有 1 次成功派发（entry→middle）
    dispatched = [ev for ev in events if ev.msg.kind == "skill_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0].msg.data["skill_id"] == "middle"

    # leaf 派发应失败：tool_call_completed.is_error=True 且 output 含
    # max_depth_exceeded
    tool_done = [
        ev for ev in events
        if ev.msg.kind == "tool_call_completed"
        and ev.msg.data.get("name") == "call_skill"
    ]
    # 至少 2 次 call_skill：一次成功（middle）一次失败（leaf）
    assert len(tool_done) >= 2, _kinds(events)
    failed = [t for t in tool_done if t.msg.data.get("is_error")]
    assert failed, "expected at least one failed call_skill"
    assert any(
        "max_depth_exceeded" in (t.msg.data.get("output") or "")
        for t in failed
    )


# ====================================================================
# 3. 运行时环检测：child 试图回调 entry
# ====================================================================

@pytest.mark.asyncio
async def test_runtime_cycle_detected_when_child_calls_entry(
    tmp_path: Path,
) -> None:
    """middle 试图 call_skill('entry') → runtime cycle_detected。

    Mock 4 个 turn：
        1. entry: call_skill(middle)
        2. middle: call_skill(entry) —— 应被拒
        3. middle turn 2: 收到 error 后终结
        4. entry turn 2: 终结
    """
    skills = _build_skills(tmp_path)
    threads = tmp_path / "threads"
    threads.mkdir()

    client = MockClient(turns=[
        MockTurn(
            text="派发 middle",
            tool_calls=[{
                "id": "tc_mid",
                "name": "call_skill",
                "arguments": '{"skill_id": "middle", "args": {}}',
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        MockTurn(
            text="尝试回调 entry（应被环检测拒绝）",
            tool_calls=[{
                "id": "tc_cycle",
                "name": "call_skill",
                "arguments": '{"skill_id": "entry", "args": {}}',
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        MockTurn(
            text="middle 退出：环已被拒",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        MockTurn(
            text="entry 收尾",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads, model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s-cycle", entry_skill_id="entry",
    )
    sub_id, events = await _run_until_outer_done(
        engine, taifeng.UserMessage(text="cycle test"),
    )
    await pool.close()

    assert events[-1].msg.kind == "turn_completed", _kinds(events)

    # 唯一一次成功派发：entry→middle
    dispatched = [ev for ev in events if ev.msg.kind == "skill_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0].msg.data["skill_id"] == "middle"

    # 环错误：call_skill 输出含 cycle_detected 或等价描述
    tool_done = [
        ev for ev in events
        if ev.msg.kind == "tool_call_completed"
        and ev.msg.data.get("name") == "call_skill"
    ]
    failed = [t for t in tool_done if t.msg.data.get("is_error")]
    assert failed, "expected one failed call_skill (cycle)"
    assert any(
        "cycle" in (t.msg.data.get("output") or "").lower()
        for t in failed
    )
