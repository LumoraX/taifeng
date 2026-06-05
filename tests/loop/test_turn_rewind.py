"""Turn 内任意节点可寻址 rewind 测试。

覆盖统一回访节点模型(turn_root / iteration / dispatch)与 Rewind Op:
- Slice 1：Rewind Op 数据契约(本文件起步)
- Slice 2：RewindCheckpoint 记录(iteration + dispatch 节点)
- Slice 4：_handle_rewind 行为(re_reason / retry_tool / 拒绝路径 / R2/R4/R5)

设计见 docs/superpowers/specs/2026-06-05-addressable-dispatch-rewind-design.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.loop.submission import Rewind, Submission


async def _run_to_end(engine: taifeng.AgentEngine, text: str) -> None:
    """提交一条 user message 并消费事件到 turn 终结。"""
    sub_id = await engine.submit(taifeng.UserMessage(text=text))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break


def test_rewind_op_defaults_and_discriminator() -> None:
    """Rewind Op：kind 鉴别字段固定、mode 默认 re_reason、new_args 默认 None。"""
    op = Rewind(node_id="it3")
    assert op.kind == "rewind"
    assert op.mode == "re_reason"
    assert op.new_args is None
    assert op.node_id == "it3"


def test_rewind_op_accepts_retry_tool_with_args() -> None:
    """retry_tool 模式 + new_args 显式入参可正常构造。"""
    op = Rewind(node_id="disp2", mode="retry_tool", new_args={"skill_id": "x"})
    assert op.mode == "retry_tool"
    assert op.new_args == {"skill_id": "x"}


def test_rewind_op_in_submission_union() -> None:
    """Rewind 并入 Op union —— Submission 能按 discriminator 反序列化它。"""
    sub = Submission.model_validate(
        {"op": {"kind": "rewind", "node_id": "disp0", "mode": "retry_tool"}}
    )
    assert isinstance(sub.op, Rewind)
    assert sub.op.node_id == "disp0"
    assert sub.op.mode == "retry_tool"


def test_rewind_op_rejects_bad_mode() -> None:
    """非法 mode 必须被 pydantic 拒绝(禁 silent fallback)。"""
    with pytest.raises(ValueError):
        Rewind(node_id="x", mode="bogus")  # type: ignore[arg-type]


# ── Slice 2：回访节点表记录(iteration + dispatch)──────────────────────


@pytest.mark.asyncio
async def test_checkpoints_cover_iterations_and_dispatches(
    skills_dir: Path, threads_dir: Path
) -> None:
    """自治 turn 跑 3 圈(前 2 圈各 1 次 read_skill 派发)→ 节点表含 3 iteration + 2 dispatch。

    并验证 dispatch 节点的 re_reason 切点(history_len)== 所属 iteration 的 history_len,
    inner_history_len(retry_tool 切点)严格大于 re_reason 切点(夹在 fc 与 fco 之间)。
    """
    client = MockClient(turns=[
        MockTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="圈2", tool_calls=[
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="收尾"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_rw2", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")

    nodes = engine.rewind_nodes()
    its = [n for n in nodes if n.kind == "iteration"]
    disps = [n for n in nodes if n.kind == "dispatch"]
    assert len(its) == 3, f"应有 3 个 iteration 节点, 实得 {len(its)}"
    assert len(disps) == 2, f"应有 2 个 dispatch 节点, 实得 {len(disps)}"
    # node_id 唯一
    assert len({n.node_id for n in nodes}) == len(nodes)
    for d in disps:
        it = next(n for n in its if n.iteration_index == d.iteration_index)
        assert d.history_len == it.history_len, "dispatch re_reason 切点应等于所属 iteration 采样前"
        assert d.inner_history_len is not None
        assert d.inner_history_len > d.history_len, "retry_tool 切点应在 fc 之后"
        assert d.target_id == "read_skill"
    await pool.close()
