"""loop 层对「空 completion」的处置 —— 决策 B：空不是错误。

只有 LLM **显式报错**（如 provider 上报的 finish_reason=content_filter）才是错误。
模型「没产出内容」本身不是错误：一个无 text、无 tool call 的终止 turn 视为正常完成
（`success=True`、`final_text=""`），子 skill 派发回 `ToolResult.ok("")`，父 turn 拿到
空结果**继续**即可 —— loop 层不臆断空为异常（空可能源于 prompt/skill，归因交业务侧）。

本用例钉死该约定，防止回归到「把空判成 LLM 错误」。

参照：tests/skill/test_composite_e2e.py 的 EnginePool + subscribe_all 模式。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from tests.conftest import run_until_root_done

if TYPE_CHECKING:
    from pathlib import Path

_ENTRY_BODY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [leaf]
tool_names: []
max_call_depth: 6
---
# entry body
"""

_LEAF_BODY = """---
name: leaf
description: 叶子层
version: 1.0.0
type: atomic
---
# leaf body
"""


def _build_skills(tmp_path: Path) -> Path:
    """构造 entry(composite, child=[leaf]) → leaf(atomic) 两层 skill。"""
    skills = tmp_path / "skills"
    d = skills / "entry"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_ENTRY_BODY, encoding="utf-8")
    leaf = skills / "leaf"
    leaf.mkdir(parents=True)
    (leaf / "SKILL.md").write_text(_LEAF_BODY, encoding="utf-8")
    return skills


async def _run_until_root_done(
    engine: taifeng.AgentEngine,
    message: taifeng.UserMessage,
    *,
    deadline_seconds: float = 10.0,
) -> list:
    """提交 message 并等最外层 turn 终态，返回该 submission 的全部事件。

    原实现用 skill_dispatched/skill_returned 计数深度推断「回到根」；现改用
    conftest 的共享 helper，判据换成 loop/event.py 明文契约的 is_root——终态
    事件本就带该字段，不必绕道计数。
    """
    return await run_until_root_done(
        engine, message, deadline_seconds=deadline_seconds
    )


async def test_empty_subskill_completion_is_tolerated(tmp_path: Path, caplog) -> None:
    """leaf 子 turn 返回空（无 text + 无 tool call）→ 视为正常完成：
    call_skill **不**标 is_error（回 ok），父 turn 继续，且不记 ERROR turn failed。"""
    caplog.set_level(logging.DEBUG, logger="taifeng.loop.turn")
    skills = _build_skills(tmp_path)
    threads = tmp_path / "threads"
    threads.mkdir()

    client = SimClient(turns=[
        # entry turn 1 → 派发 leaf
        SimTurn(
            text="派发到 leaf",
            tool_calls=[{
                "id": "tc_leaf",
                "name": "call_skill",
                "arguments": '{"skill_id": "leaf", "args": {"q": "x"}}',
            }],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
        # leaf turn 1 → 空回复（无显式错误）：决策 B 视为正常空结果
        SimTurn(text="", usage=TokenUsage(input_tokens=10, output_tokens=0, total_tokens=10)),
        # entry turn 2 → 拿到空结果后继续给最终文本
        SimTurn(
            text="entry 收尾：子结果为空，已据现有信息继续",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s-empty", entry_skill_id="entry")
    events = await _run_until_root_done(engine, taifeng.UserMessage(text="go"))
    await pool.close()

    # 最外层 turn 应「完成」而非「失败」（空不阻断任务）
    assert events[-1].msg.kind == "turn_completed", [ev.msg.kind for ev in events]

    # call_skill(leaf) 的 tool_call_completed 不应标 is_error —— 空 = ok 空结果
    call_skill_done = [
        ev for ev in events
        if ev.msg.kind == "tool_call_completed"
        and ev.msg.data.get("name") == "call_skill"
    ]
    assert call_skill_done, [ev.msg.kind for ev in events]
    leaf_call = call_skill_done[0]
    assert leaf_call.msg.data.get("is_error") is False, (
        f"空子 turn 应视为正常空结果（ok），不得判 error：{leaf_call.msg.data}"
    )

    # 不得把空当成崩溃记 ERROR turn failed
    error_turn_failed = [
        r for r in caplog.records
        if r.levelno >= logging.ERROR and "turn failed" in r.getMessage()
    ]
    assert not error_turn_failed, (
        f"空 completion 不是错误，不应记 ERROR：{[r.getMessage() for r in error_turn_failed]}"
    )
