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


def _is_root_end(ev: object) -> bool:
    """事件是否为 root turn 的终结(call_skill 子 turn 也发 turn_completed,需按 is_root 区分)。"""
    kind = ev.msg.kind  # type: ignore[attr-defined]
    if kind not in ("turn_completed", "turn_failed"):
        return False
    data = ev.msg.data if hasattr(ev.msg, "data") else {}  # type: ignore[attr-defined]
    return bool(data.get("is_root"))


async def _consume_to_root_end(engine: taifeng.AgentEngine, sub_id: str) -> tuple[
    list[str], str, dict
]:
    """消费某 submission 的事件直到 **root turn** 终结,返回 (kinds, assistant 文本, turn_rewound.data)。

    必须走 ``subscribe_all()`` 而非 ``subscribe(sub_id)``:后者在该 submission 的**任意**
    turn_completed 即终止(含 call_skill **子 turn** 的 is_root=False 完成),会在父续推前
    断流(见 engine.subscribe 的终止集合)。这里只在 root 完成时停(pipeline.py 同款)。
    """
    kinds: list[str] = []
    text_parts: list[str] = []
    rewound: dict = {}
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        kinds.append(ev.msg.kind)
        data = ev.msg.data if hasattr(ev.msg, "data") else {}
        if ev.msg.kind == "assistant_text" and data.get("delta"):
            text_parts.append(str(data["delta"]))
        if ev.msg.kind == "turn_rewound":
            rewound = dict(data)
        if _is_root_end(ev):
            break
    return kinds, "".join(text_parts), rewound


async def _run_to_end(engine: taifeng.AgentEngine, text: str) -> None:
    """提交一条 user message 并消费事件到 root turn 终结。

    turn_completed 在 runner.run() **内部**发出,而 engine 的状态(history /
    rewind_checkpoints)回写在 run() **返回后**;故收尾轮询等节点表落地(仓库既有
    settle 模式),保证 rewind_nodes() 对调用方可见。
    """
    import asyncio
    sub_id = await engine.submit(taifeng.UserMessage(text=text))
    await _consume_to_root_end(engine, sub_id)
    for _ in range(100):
        if engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)


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


# ── Slice 3：rewind 事件类型 + rewind_checkpoint_recorded 发射 ────────────


def test_rewind_event_classes_have_kinds() -> None:
    """三个 rewind 事件类的 discriminator 固定。"""
    from taifeng.loop.event import (
        RewindCheckpointRecorded,
        RewindRejected,
        TurnRewound,
    )

    assert RewindCheckpointRecorded().kind == "rewind_checkpoint_recorded"
    assert TurnRewound().kind == "turn_rewound"
    assert RewindRejected().kind == "rewind_rejected"


@pytest.mark.asyncio
async def test_rewind_checkpoint_recorded_events_emitted(
    skills_dir: Path, threads_dir: Path
) -> None:
    """root turn 每记一个回访节点 → 发一条 rewind_checkpoint_recorded 事件(R3)。"""
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
    engine = await pool.get_or_create(session_id="s_rw3", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    kinds: list[str] = []
    async for ev in engine.subscribe(sub_id):
        kinds.append(ev.msg.kind)
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break

    recorded = [k for k in kinds if k == "rewind_checkpoint_recorded"]
    # 每个节点一条事件;节点表 = 3 iteration + 2 dispatch
    assert len(recorded) == len(engine.rewind_nodes())
    assert len(recorded) == 5
    await pool.close()


# ── Slice 4：_handle_rewind 行为(拒绝路径 + re_reason 重推)──────────────


async def _drain(engine: taifeng.AgentEngine, sub_id: str) -> tuple[list[str], str, dict]:
    """消费某 submission 的事件到 **root turn** 终结(委托 _consume_to_root_end)。

    走 subscribe_all + is_root 过滤,避免 call_skill 子 turn 完成时 subscribe 提前断流。
    """
    return await _consume_to_root_end(engine, sub_id)


@pytest.mark.asyncio
async def test_rewind_unknown_node_rejected(skills_dir: Path, threads_dir: Path) -> None:
    """node_id 不存在 → rewind_rejected(unknown_node),history 不动。"""
    client = MockClient(turns=[MockTurn(text="hi")])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_rej", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")
    before = len(engine.history_snapshot())

    sub_id = await engine.submit(Rewind(node_id="does-not-exist"))
    rejected = None
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind == "rewind_rejected":
            rejected = dict(ev.msg.data)
            break
    assert rejected is not None
    assert rejected["reason"] == "unknown_node"
    assert len(engine.history_snapshot()) == before, "拒绝路径不得改 history"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_re_reason_truncates_and_redrives(
    skills_dir: Path, threads_dir: Path
) -> None:
    """rewind 到 iteration 节点(re_reason)→ 截到该圈采样前 + 重采样,走出新路径。"""
    client = MockClient(turns=[
        MockTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="圈2", tool_calls=[
            {"id": "c1", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="原收尾"),
        # 重推消费:rewind 到 it2 后重采样,直接无工具收尾(证明走了新路径)
        MockTurn(text="REDRIVE-DONE"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_rr", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")

    it2 = next(n for n in engine.rewind_nodes()
               if n.kind == "iteration" and n.iteration_index == 2)
    cut = it2.history_len

    sub_id = await engine.submit(Rewind(node_id="t1:it2", mode="re_reason"))
    kinds, text, rewound = await _drain(engine, sub_id)

    assert "turn_rewound" in kinds
    assert rewound["node_id"] == "t1:it2"
    assert rewound["mode"] == "re_reason"
    assert rewound["cut_index"] == cut
    assert "REDRIVE-DONE" in text, "重采样应走出新文本"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_mode_kind_mismatch_rejected(
    skills_dir: Path, threads_dir: Path
) -> None:
    """对 iteration 节点用 retry_tool → mode_kind_mismatch 拒绝。"""
    client = MockClient(turns=[
        MockTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="收尾"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_mk", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")

    sub_id = await engine.submit(Rewind(node_id="t1:it1", mode="retry_tool"))
    rejected = None
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind == "rewind_rejected":
            rejected = dict(ev.msg.data)
            break
    assert rejected is not None
    assert rejected["reason"] == "mode_kind_mismatch"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_dispatch_retry_tool_reruns_and_continues(
    skills_dir: Path, threads_dir: Path
) -> None:
    """retry_tool：保留 assistant 的 function_call,补跑该工具,再续推到新结果。"""
    client = MockClient(turns=[
        MockTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="原收尾"),
        # retry_tool 后:seed 补跑 read_skill(不耗 MockTurn),续推消费这条
        MockTurn(text="RETRY-TOOL-DONE"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_rt", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")

    disp0 = next(n for n in engine.rewind_nodes() if n.kind == "dispatch")
    sub_id = await engine.submit(Rewind(node_id=disp0.node_id, mode="retry_tool"))
    kinds, text, rewound = await _drain(engine, sub_id)

    assert rewound["node_id"] == disp0.node_id
    assert rewound["mode"] == "retry_tool"
    assert rewound["cut_index"] == disp0.inner_history_len
    assert "RETRY-TOOL-DONE" in text, "续推应走出新文本"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_retry_tool_reruns_call_skill(
    skills_dir: Path, threads_dir: Path
) -> None:
    """头号场景:retry_tool 重跑自治链里的一次 call_skill —— 子 skill 真被重跑、父续推。"""
    client = MockClient(turns=[
        # code-reviewer 圈1:派发 call_skill(style-checker)
        MockTurn(text="派发风格审查", tool_calls=[{
            "id": "cs0", "name": "call_skill",
            "arguments": '{"skill_id":"style-checker","reason":"审查风格","args":{}}'}]),
        MockTurn(text="风格结论-A"),   # style-checker 子 turn(首跑)
        MockTurn(text="综合-A"),        # code-reviewer 圈2 收尾
        # —— retry_tool 后:seed 重跑 call_skill → 新子 turn + 父续推 ——
        MockTurn(text="风格结论-B"),   # style-checker 子 turn(重跑)
        MockTurn(text="综合-B"),        # code-reviewer 续推
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_csrt", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "审查这段代码")

    disp = next(n for n in engine.rewind_nodes()
                if n.kind == "dispatch" and n.target_id == "call_skill")
    sub_id = await engine.submit(Rewind(node_id=disp.node_id, mode="retry_tool"))
    kinds, text, rewound = await _drain(engine, sub_id)

    assert rewound["node_id"] == disp.node_id
    assert rewound["mode"] == "retry_tool"
    assert "风格结论-B" in text, "子 skill 应被真正重跑(走新子 turn)"
    assert "综合-B" in text, "父 skill 应基于新子结论续推"
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_dispatch_re_reason_cuts_to_iteration(
    skills_dir: Path, threads_dir: Path
) -> None:
    """对 dispatch 节点用 re_reason → 截点归一到所属 iteration 采样前(非 inner 切点)。"""
    client = MockClient(turns=[
        MockTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="原收尾"),
        MockTurn(text="RE-REASON-DONE"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_drr", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")

    nodes = engine.rewind_nodes()
    disp0 = next(n for n in nodes if n.kind == "dispatch")
    it1 = next(n for n in nodes if n.kind == "iteration" and n.iteration_index == 1)

    sub_id = await engine.submit(Rewind(node_id=disp0.node_id, mode="re_reason"))
    _, text, rewound = await _drain(engine, sub_id)

    # dispatch.re_reason 截点 == 所属 iteration 采样前(history_len),不是 inner_history_len
    assert rewound["cut_index"] == disp0.history_len == it1.history_len
    assert rewound["cut_index"] != disp0.inner_history_len
    assert "RE-REASON-DONE" in text
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_retry_tool_new_args_rewrites_call(
    skills_dir: Path, threads_dir: Path
) -> None:
    """retry_tool + new_args:改写悬空 fc 的入参后重跑该工具,续推到新结果。"""
    client = MockClient(turns=[
        MockTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="原收尾"),
        MockTurn(text="NEW-ARGS-DONE"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_na", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")

    disp0 = next(n for n in engine.rewind_nodes() if n.kind == "dispatch")
    sub_id = await engine.submit(Rewind(
        node_id=disp0.node_id, mode="retry_tool",
        new_args={"skill_id": "code-reviewer"},  # 换 read_skill 的目标
    ))
    _, text, rewound = await _drain(engine, sub_id)

    assert rewound["mode"] == "retry_tool"
    assert "NEW-ARGS-DONE" in text
    # 内存 history 中该 fc 的 arguments 已被改写为新 skill_id
    fc = next(it for it in engine.history_snapshot()
              if it.kind == "function_call" and it.payload.get("call_id") == disp0.call_id)
    assert "code-reviewer" in fc.payload.get("arguments", "")
    await pool.close()


# ── Slice 5：R2 cache 失效如实上报为 expected ──────────────────────────


@pytest.mark.asyncio
async def test_rewind_cache_break_marked_expected(
    skills_dir: Path, threads_dir: Path
) -> None:
    """rewind 蓄意回退 cache_anchor → 该失效记为 expected,不计入 unexpected_breaks(R2)。"""
    from taifeng.llm.types import TokenUsage

    client = MockClient(turns=[
        MockTurn(text="原", cache_read=100, usage=TokenUsage(input_tokens=100)),
        # 重推首采样:cache_read 跌破 → 若不标 expected 会误记 unexpected break
        MockTurn(text="重推", cache_read=10, usage=TokenUsage(input_tokens=100)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_cache", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")
    assert engine.cache_stats.unexpected_cache_breaks == 0

    sub_id = await engine.submit(Rewind(node_id="t1:it1", mode="re_reason"))
    await _drain(engine, sub_id)

    # rewind 导致的 cache 失效是预期内的,不得计入 unexpected
    assert engine.cache_stats.unexpected_cache_breaks == 0
    await pool.close()


@pytest.mark.asyncio
async def test_rewind_store_append_only_preserved(
    skills_dir: Path, threads_dir: Path
) -> None:
    """rewind 只在内存截 history;store(JSONL)append-only,旧 items 不被物理删(R5)。"""
    client = MockClient(turns=[
        MockTurn(text="圈1", tool_calls=[
            {"id": "c0", "name": "read_skill", "arguments": '{"skill_id":"style-checker"}'}]),
        MockTurn(text="ORIGINAL-TAIL"),
        MockTurn(text="REDRIVEN"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client, compressors=[],
    )
    engine = await pool.get_or_create(session_id="s_ap", entry_skill_id="code-reviewer")
    await _run_to_end(engine, "go")

    # 找到 thread 的 JSONL 落盘文件,记录 rewind 前的行数与内容
    jsonl = next(iter(threads_dir.rglob("*.jsonl")))
    before_lines = jsonl.read_text(encoding="utf-8").splitlines()
    before_blob = "\n".join(before_lines)
    assert "ORIGINAL-TAIL" in before_blob

    await _drain(
        engine,
        await engine.submit(Rewind(node_id="t1:it1", mode="re_reason")),
    )

    after_blob = jsonl.read_text(encoding="utf-8")
    # append-only:旧内容仍在(含被内存截断掉的 ORIGINAL-TAIL),且只增不减
    assert "ORIGINAL-TAIL" in after_blob, "旧 items 不得被物理删"
    assert "[rewind]" in after_blob, "应追加 rewind marker"
    assert len(after_blob.splitlines()) >= len(before_lines)
    await pool.close()
