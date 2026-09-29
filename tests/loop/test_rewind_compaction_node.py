"""压缩作为回访节点（ADR 0081）：回到某次压缩之前。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    compacted,
    function_call,
    function_call_output,
    system_injection,
    user_message,
)
from taifeng.conversation.reconstruct import (
    reconstruct_before_compaction,
    reconstruct_logical_history,
)
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.rewind import CompactionUndo, awaits_model, derive_rewind_log
from taifeng.loop.submission import CompactNow, Rewind
from tests.conftest import GUARD_TIMEOUT_SECONDS, wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

_T = "thr"


def _user(text: str) -> ResponseItem:
    return user_message(text, thread_id=_T)


def _assistant(text: str) -> ResponseItem:
    return assistant_message(text, thread_id=_T, model="m")


def _placeholder(start: int, end: int) -> ResponseItem:
    return compacted(
        "摘要", thread_id=_T, replaced_range=(start, end), cache_invalidated=True
    )


def _raw_with_compaction() -> tuple[list[ResponseItem], ResponseItem]:
    """两轮对话 → 压缩（折叠前三项）→ 第三轮。返回 (raw, placeholder)。"""
    before = [_user("一"), _assistant("答一"), _user("二"), _assistant("答二")]
    placeholder = _placeholder(0, 3)
    after = [_user("三"), _assistant("答三")]
    return [*before, placeholder, *after], placeholder


# ------------------------------------------------------------------
# 节点表
# ------------------------------------------------------------------


def test_compacted_item_yields_a_compaction_node() -> None:
    raw, placeholder = _raw_with_compaction()
    logical = reconstruct_logical_history(raw)

    nodes = [n for n in derive_rewind_log(logical) if n.kind == "compaction"]

    assert len(nodes) == 1
    node = nodes[0]
    assert node.node_id == "t0:cmp0"
    assert node.target_id == placeholder.id
    assert node.history_len == logical.index(placeholder) == 0
    assert node.call_id is None
    assert node.inner_history_len is None


def test_compaction_nodes_are_numbered_per_turn() -> None:
    logical = [
        _user("一"), _placeholder(1, 3), _assistant("答一"), _placeholder(3, 5),
        _user("二"), _placeholder(5, 7),
    ]
    ids = [n.node_id for n in derive_rewind_log(logical) if n.kind == "compaction"]
    assert ids == ["t1:cmp0", "t1:cmp1", "t2:cmp0"]


def test_other_node_ids_are_unaffected_by_compaction_nodes() -> None:
    """压缩节点不占用采样 / 派发节点的编号。"""
    raw, placeholder = _raw_with_compaction()
    logical = reconstruct_logical_history(raw)
    without = [i for i in logical if i is not placeholder]

    ids = [n.node_id for n in derive_rewind_log(logical) if n.kind != "compaction"]

    assert ids == [n.node_id for n in derive_rewind_log(without)]
    assert ids == ["t0:it1", "t1:it1"]


# ------------------------------------------------------------------
# 还原到压缩之前
# ------------------------------------------------------------------


def test_restore_returns_state_before_the_compaction() -> None:
    raw, placeholder = _raw_with_compaction()

    restored = reconstruct_before_compaction(raw, placeholder.id)

    assert restored == raw[:4]


def test_restore_strips_side_items_written_by_the_compaction() -> None:
    """压缩动作自己写下的抢救摘要与钉回项不属于「压缩之前」。"""
    base = [_user("一"), _assistant("答一"), _user("二"), _assistant("答二")]
    salvage = system_injection("要点", thread_id=_T, source="memory_pre_evict")
    pinned = system_injection("清单", thread_id=_T, source="pinned:todo")
    placeholder = _placeholder(0, 3)
    raw = [*base, salvage, pinned, placeholder]

    assert reconstruct_before_compaction(raw, placeholder.id) == base


def test_restore_keeps_ordinary_injections() -> None:
    base = [_user("一"), _assistant("答一")]
    note = system_injection("业务注入", thread_id=_T, source="business")
    placeholder = _placeholder(0, 2)

    restored = reconstruct_before_compaction([*base, note, placeholder], placeholder.id)

    assert restored == [*base, note]


def test_restore_honours_earlier_compactions() -> None:
    """撤销第二次压缩：回到第一次压缩之后、第二次之前。"""
    first = _placeholder(0, 2)
    second = _placeholder(0, 3)
    raw = [_user("一"), _assistant("答一"), first, _user("二"), _assistant("答二"), second]

    restored = reconstruct_before_compaction(raw, second.id)

    assert restored == [first, raw[3], raw[4]]


def test_restore_unknown_compaction_fails_loudly() -> None:
    raw, _ = _raw_with_compaction()
    with pytest.raises(ValueError, match="compaction"):
        reconstruct_before_compaction(raw, "item_missing")


def test_undo_marker_is_replayed_on_cold_reconstruct() -> None:
    raw, placeholder = _raw_with_compaction()
    restored = reconstruct_before_compaction(raw, placeholder.id)
    undo = CompactionUndo(
        restored=tuple(restored), compaction_id=placeholder.id, first_changed_index=0
    )
    marker = system_injection(
        "[rewind]", thread_id=_T, source="rewind", extra=undo.marker_extra()
    )
    later = _user("四")

    logical = reconstruct_logical_history([*raw, marker, later])

    assert undo.marker_extra() == {"cut_index": 4, "undo_compaction": placeholder.id}
    assert logical == [*restored, later]
    assert not [i for i in logical if i.kind == "compacted"]


def test_undo_marker_with_unknown_compaction_fails_loudly() -> None:
    raw, _ = _raw_with_compaction()
    marker = system_injection(
        "[rewind]", thread_id=_T, source="rewind",
        extra={"cut_index": 4, "undo_compaction": "item_missing"},
    )
    with pytest.raises(ValueError, match="undo_compaction"):
        reconstruct_logical_history([*raw, marker])


def test_undo_marker_with_inconsistent_cut_index_fails_loudly() -> None:
    raw, placeholder = _raw_with_compaction()
    marker = system_injection(
        "[rewind]", thread_id=_T, source="rewind",
        extra={"cut_index": 3, "undo_compaction": placeholder.id},
    )
    with pytest.raises(ValueError, match="cut_index"):
        reconstruct_logical_history([*raw, marker])


# ------------------------------------------------------------------
# 是否轮到模型说话
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([], False),
        ([_user("问")], True),
        ([_user("问"), _assistant("答")], False),
        ([
            _user("问"),
            function_call(call_id="c", name="t", arguments="{}", thread_id=_T),
            function_call_output(call_id="c", output="o", thread_id=_T),
        ], True),
        ([_user("问"), system_injection("清单", thread_id=_T, source="pinned:todo")], True),
        ([_placeholder(0, 2)], False),
    ],
)
def test_awaits_model(items: list[ResponseItem], expected: bool) -> None:
    assert awaits_model(items) is expected


# ------------------------------------------------------------------
# 引擎级
# ------------------------------------------------------------------


def _compressor() -> list[Any]:
    summary = SimClient(turns=[
        SimTurn(text="## 摘要", usage=TokenUsage(input_tokens=100, output_tokens=10))
        for _ in range(4)
    ])
    return [HandoffCompactionStrategy(model_client=summary)]


async def _until(engine: taifeng.AgentEngine, sub_id: str, *kinds: str) -> Any:
    """消费到该 submission 出现给定事件之一；超时即失败。"""

    async def collect() -> Any:
        async for ev in engine.subscribe_all():
            if ev.submission_id == sub_id and ev.msg.kind in kinds:
                return ev.msg
        raise AssertionError("stream ended early")

    return await asyncio.wait_for(collect(), timeout=GUARD_TIMEOUT_SECONDS)


async def _drive(engine: taifeng.AgentEngine, texts: list[str]) -> None:
    for text in texts:
        sub_id = await engine.submit(taifeng.UserMessage(text=text))
        msg = await _until(engine, sub_id, "turn_completed", "turn_failed")
        assert msg.kind == "turn_completed"


async def _compacted_engine(
    skills_dir: Path, threads_dir: Path, *extra_turns: SimTurn,
) -> tuple[taifeng.EnginePool, taifeng.AgentEngine, list[ResponseItem]]:
    """三轮对话后手动压缩；返回 (pool, engine, 压缩前的 history)。"""
    client = SimClient(turns=[SimTurn(text=f"答{i}") for i in range(3)] + list(extra_turns))
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=_compressor(),
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    await _drive(engine, ["a", "b", "c"])
    await wait_for_condition(lambda: engine.rewind_nodes(), message="节点表未落地")
    before = list(engine.history_snapshot())
    sub_id = await engine.submit(CompactNow(force=True))
    done = await _until(engine, sub_id, "compaction_completed", "turn_failed")
    assert done.kind == "compaction_completed" and done.data["success"] is True
    return pool, engine, before


def _compaction_node(engine: taifeng.AgentEngine) -> Any:
    return next(n for n in engine.rewind_nodes() if n.kind == "compaction")


async def test_manual_compaction_appears_in_node_table(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool, engine, _ = await _compacted_engine(skills_dir, threads_dir)

    await wait_for_condition(
        lambda: any(n.kind == "compaction" for n in engine.rewind_nodes()),
        message="压缩节点未进节点表",
    )
    node = _compaction_node(engine)
    placeholder = next(i for i in engine.history_snapshot() if i.kind == "compacted")
    assert node.target_id == placeholder.id
    await pool.close()


async def test_restore_brings_back_the_history_before_compaction(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool, engine, before = await _compacted_engine(skills_dir, threads_dir)
    await wait_for_condition(
        lambda: any(n.kind == "compaction" for n in engine.rewind_nodes()),
        message="压缩节点未进节点表",
    )
    node = _compaction_node(engine)

    sub_id = await engine.submit(Rewind(node_id=node.node_id, mode="restore"))
    rewound = await _until(engine, sub_id, "turn_rewound", "rewind_rejected")

    assert rewound.kind == "turn_rewound", rewound.data
    assert rewound.data["mode"] == "restore"
    assert rewound.data["node_kind"] == "compaction"
    assert rewound.data["redriven"] is False
    assert rewound.data["undo_compaction"] == node.target_id
    assert [i.id for i in engine.history_snapshot()] == [i.id for i in before]
    assert not any(n.kind == "compaction" for n in engine.rewind_nodes())
    assert [n.node_id for n in engine.rewind_nodes()] == ["t1:it1", "t2:it1", "t3:it1"]
    await pool.close()


async def test_conversation_continues_after_restore(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool, engine, before = await _compacted_engine(
        skills_dir, threads_dir, SimTurn(text="答3"),
    )
    await wait_for_condition(
        lambda: any(n.kind == "compaction" for n in engine.rewind_nodes()),
        message="压缩节点未进节点表",
    )
    sub_id = await engine.submit(Rewind(node_id=_compaction_node(engine).node_id, mode="restore"))
    await _until(engine, sub_id, "turn_rewound", "rewind_rejected")

    await _drive(engine, ["d"])

    ids = [i.id for i in engine.history_snapshot()]
    assert ids[: len(before)] == [i.id for i in before]
    assert [i.kind for i in engine.history_snapshot()][len(before):] == [
        "user_message", "assistant_message",
    ]
    await pool.close()


async def test_cold_reload_after_restore_matches_hot_history(
    skills_dir: Path, threads_dir: Path,
) -> None:
    pool, engine, _ = await _compacted_engine(skills_dir, threads_dir)
    await wait_for_condition(
        lambda: any(n.kind == "compaction" for n in engine.rewind_nodes()),
        message="压缩节点未进节点表",
    )
    sub_id = await engine.submit(Rewind(node_id=_compaction_node(engine).node_id, mode="restore"))
    await _until(engine, sub_id, "turn_rewound", "rewind_rejected")
    hot = [(i.kind, i.id) for i in engine.history_snapshot()]
    thread_id = engine.thread_id
    await pool.close()

    pool2 = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=SimClient(turns=[]), compressors=[],
    )
    cold = await pool2.get_or_create(
        session_id="s", entry_skill_id="code-reviewer", resume_thread_id=thread_id
    )

    assert [(i.kind, i.id) for i in cold.history_snapshot()] == hot
    await pool2.close()


async def test_re_reason_on_idle_compaction_has_nothing_to_redrive(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """压缩发生在两轮之间：压缩前最后一项是模型的回答，没有可重推的采样。"""
    pool, engine, _ = await _compacted_engine(skills_dir, threads_dir)
    await wait_for_condition(
        lambda: any(n.kind == "compaction" for n in engine.rewind_nodes()),
        message="压缩节点未进节点表",
    )
    snapshot = [i.id for i in engine.history_snapshot()]

    sub_id = await engine.submit(
        Rewind(node_id=_compaction_node(engine).node_id, mode="re_reason")
    )
    rejected = await _until(engine, sub_id, "turn_rewound", "rewind_rejected")

    assert rejected.kind == "rewind_rejected"
    assert rejected.data["reason"] == "nothing_to_redrive"
    assert [i.id for i in engine.history_snapshot()] == snapshot
    await pool.close()


@pytest.mark.parametrize(
    ("node", "mode"),
    [("compaction", "retry_tool"), ("iteration", "restore")],
)
async def test_mode_and_node_kind_must_match(
    skills_dir: Path, threads_dir: Path, node: str, mode: str,
) -> None:
    pool, engine, _ = await _compacted_engine(skills_dir, threads_dir)
    await wait_for_condition(
        lambda: any(n.kind == "compaction" for n in engine.rewind_nodes()),
        message="压缩节点未进节点表",
    )
    target = next(n for n in engine.rewind_nodes() if n.kind == node)

    sub_id = await engine.submit(Rewind(node_id=target.node_id, mode=mode))  # type: ignore[arg-type]
    rejected = await _until(engine, sub_id, "turn_rewound", "rewind_rejected")

    assert rejected.kind == "rewind_rejected"
    assert rejected.data["reason"] == "mode_kind_mismatch"
    await pool.close()


async def test_re_reason_on_pre_turn_compaction_restores_and_redrives(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """压缩发生在某轮采样之前：还原后最后一条是用户消息，从那里重推。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=SimClient(turns=[SimTurn(text="重答三")]), compressors=[],
    )
    thread_id = await pool.store.create_thread(
        cwd=None, entry_skill_id="code-reviewer", source="test", extra={},
    )

    def at(item: ResponseItem) -> ResponseItem:
        return item.model_copy(update={"thread_id": thread_id})

    before = [at(_user("一")), at(_assistant("答一")), at(_user("二")),
              at(_assistant("答二")), at(_user("三"))]
    placeholder = at(_placeholder(0, 4))
    for item in [*before, placeholder, at(_assistant("答三"))]:
        await pool.store.append(item)
    engine = await pool.get_or_create(
        session_id="s", entry_skill_id="code-reviewer", resume_thread_id=thread_id
    )
    await wait_for_condition(
        lambda: any(n.kind == "compaction" for n in engine.rewind_nodes()),
        message="压缩节点未进节点表",
    )

    sub_id = await engine.submit(
        Rewind(node_id=_compaction_node(engine).node_id, mode="re_reason")
    )
    rewound = await _until(engine, sub_id, "turn_rewound", "rewind_rejected")
    assert rewound.kind == "turn_rewound", rewound.data
    assert rewound.data["redriven"] is True
    done = await _until(engine, sub_id, "turn_completed", "turn_failed")

    assert done.kind == "turn_completed"
    history = engine.history_snapshot()
    assert [i.id for i in history[: len(before)]] == [i.id for i in before]
    assert [i.kind for i in history[len(before):]] == ["assistant_message"]
    assert history[-1].payload["text"] == "重答三"
    await pool.close()
