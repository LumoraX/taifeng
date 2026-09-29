"""并行批次里的 retry_tool（ADR 0079）。

一次采样发出多个工具调用时，对其中一个做 retry_tool 只应重跑那一个：同批其他调用的
调用记录与结果原样保留，批次之后的内容丢弃，然后续推。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    system_injection,
    tool_intent_item,
    user_message,
)
from taifeng.conversation.reconstruct import reconstruct_logical_history
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.loop.rewind import RetryCut, derive_rewind_log, plan_retry_cut
from taifeng.loop.submission import Rewind
from taifeng.loop.turn_helpers import _history_orphan_call_ids
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from pathlib import Path

_T = "thr"
_ENTRY = """---
name: fanout
description: 一次发出多个查询的入口
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [lookup]
max_call_depth: 2
---
# 入口
并发查询。
"""


def _fc(call_id: str, sample: str | None = None) -> ResponseItem:
    item = function_call(call_id=call_id, name="lookup", arguments="{}", thread_id=_T)
    if sample is None:
        return item
    return item.model_copy(update={"metadata": {"llm_sample_id": sample}})


def _fco(call_id: str, sample: str | None = None) -> ResponseItem:
    item = function_call_output(call_id=call_id, output=f"out-{call_id}", thread_id=_T)
    if sample is None:
        return item
    return item.model_copy(update={"metadata": {"origin_llm_sample_id": sample}})


def _intent(call_id: str) -> ResponseItem:
    return tool_intent_item(call_id, "lookup", "{}", thread_id=_T)


def _node(history: list[ResponseItem], call_id: str) -> Any:
    return next(n for n in derive_rewind_log(history) if n.call_id == call_id)


def _chat_batch() -> list[ResponseItem]:
    """Chat 布局：意图在前，随后逐对 (调用, 结果)；批次后还有一圈收尾。"""
    return [
        user_message("go", thread_id=_T),                      # 0
        assistant_message("查三个", thread_id=_T, model="m"),   # 1
        _intent("a"), _intent("b"), _intent("c"),              # 2 3 4
        _fc("a"), _fco("a"),                                   # 5 6
        _fc("b"), _fco("b"),                                   # 7 8
        _fc("c"), _fco("c"),                                   # 9 10
        assistant_message("收尾", thread_id=_T, model="m"),     # 11
    ]


def _responses_batch() -> list[ResponseItem]:
    """Responses 布局：调用成组在前，结果成组在后；下一圈只有调用、没有文本。"""
    return [
        user_message("go", thread_id=_T),          # 0
        _fc("a", "s1"), _fc("b", "s1"), _fc("c", "s1"),      # 1 2 3
        _fco("a", "s1"), _fco("b", "s1"), _fco("c", "s1"),   # 4 5 6
        _fc("d", "s2"), _fco("d", "s2"),                      # 7 8
    ]


# ------------------------------------------------------------------
# plan_retry_cut
# ------------------------------------------------------------------


def test_single_call_cut_equals_inner_history_len() -> None:
    """单调用批次：与既有行为逐位相同（截到调用之后、结果之前，无需另删）。"""
    history = [
        user_message("go", thread_id=_T),
        assistant_message("查", thread_id=_T, model="m"),
        _intent("a"), _fc("a"), _fco("a"),
        assistant_message("收尾", thread_id=_T, model="m"),
    ]
    node = _node(history, "a")

    cut = plan_retry_cut(history, node)

    assert cut == RetryCut(cut_index=node.inner_history_len, drop_index=None)
    assert cut.cut_index == 4


def test_chat_layout_middle_call_keeps_siblings() -> None:
    history = _chat_batch()

    cut = plan_retry_cut(history, _node(history, "b"))

    assert cut == RetryCut(cut_index=11, drop_index=8)
    kept = cut.apply(history)
    assert [(i.kind, i.payload.get("call_id")) for i in kept[5:]] == [
        ("function_call", "a"), ("function_call_output", "a"),
        ("function_call", "b"),
        ("function_call", "c"), ("function_call_output", "c"),
    ]
    assert _history_orphan_call_ids(kept) == {"b"}


def test_chat_layout_last_call_needs_no_drop() -> None:
    history = _chat_batch()

    cut = plan_retry_cut(history, _node(history, "c"))

    assert cut == RetryCut(cut_index=10, drop_index=None)
    assert _history_orphan_call_ids(cut.apply(history)) == {"c"}


def test_chat_layout_first_call() -> None:
    history = _chat_batch()

    cut = plan_retry_cut(history, _node(history, "a"))

    assert cut == RetryCut(cut_index=11, drop_index=6)
    assert _history_orphan_call_ids(cut.apply(history)) == {"a"}


def test_responses_layout_stops_at_next_sample() -> None:
    """下一圈没有文本项时，靠采样 id 划界：不把下一圈的调用算进本批。"""
    history = _responses_batch()

    cut = plan_retry_cut(history, _node(history, "b"))

    assert cut == RetryCut(cut_index=7, drop_index=5)
    kept = cut.apply(history)
    assert [i.payload.get("call_id") for i in kept[1:]] == ["a", "b", "c", "a", "c"]
    assert _history_orphan_call_ids(kept) == {"b"}


def test_batch_ends_at_injected_item() -> None:
    """批次后的注入项（如中途用户输入）属于批次之后，一并丢弃。"""
    history = _chat_batch()
    history.insert(11, system_injection("steer", thread_id=_T, source="user_steer"))

    cut = plan_retry_cut(history, _node(history, "a"))

    assert cut == RetryCut(cut_index=11, drop_index=6)


def test_call_without_output_only_cuts() -> None:
    """目标调用本就没有结果（悬空）：只截到批次末尾，补跑后追加。"""
    history = _chat_batch()
    del history[8]  # 去掉 b 的结果

    cut = plan_retry_cut(history, _node(history, "b"))

    assert cut == RetryCut(cut_index=10, drop_index=None)


def test_reused_call_id_targets_the_node_occurrence() -> None:
    """同一 turn 里 call id 被复用：按节点所在位置取那一次调用。"""
    history = [
        user_message("go", thread_id=_T),
        assistant_message("一", thread_id=_T, model="m"),
        _intent("x"), _fc("x"), _fco("x"),
        assistant_message("二", thread_id=_T, model="m"),
        _intent("x"), _fc("x"), _fco("x"),
        assistant_message("收尾", thread_id=_T, model="m"),
    ]
    first, second = [n for n in derive_rewind_log(history) if n.call_id == "x"]

    assert plan_retry_cut(history, first) == RetryCut(cut_index=4, drop_index=None)
    assert plan_retry_cut(history, second) == RetryCut(cut_index=8, drop_index=None)


def test_plan_rejects_non_dispatch_node() -> None:
    history = _chat_batch()
    iteration = next(n for n in derive_rewind_log(history) if n.kind == "iteration")
    with pytest.raises(ValueError, match="dispatch"):
        plan_retry_cut(history, iteration)


def test_plan_rejects_node_that_does_not_match_history() -> None:
    history = _chat_batch()
    node = _node(history, "b")
    with pytest.raises(ValueError, match="function_call"):
        plan_retry_cut(history[:6], node)


# ------------------------------------------------------------------
# reconstruct：冷重建与热内存一致
# ------------------------------------------------------------------


def _marker(cut: RetryCut) -> ResponseItem:
    return system_injection("[rewind]", thread_id=_T, source="rewind", extra=cut.marker_extra())


def test_marker_extra_omits_drop_index_when_not_needed() -> None:
    assert RetryCut(cut_index=4, drop_index=None).marker_extra() == {"cut_index": 4}
    assert RetryCut(cut_index=11, drop_index=8).marker_extra() == {
        "cut_index": 11, "drop_index": 8,
    }


def test_reconstruct_replays_cut_and_drop() -> None:
    history = _chat_batch()
    cut = plan_retry_cut(history, _node(history, "b"))
    new_output = _fco("b")
    raw = [*history, _marker(cut), new_output]

    logical = reconstruct_logical_history(raw)

    assert logical == [*cut.apply(history), new_output]
    assert _history_orphan_call_ids(logical) == set()


@pytest.mark.parametrize("drop", [-1, 11, 99])
def test_reconstruct_rejects_drop_index_out_of_range(drop: int) -> None:
    raw = [*_chat_batch(), _marker(RetryCut(cut_index=11, drop_index=drop))]
    with pytest.raises(ValueError, match="drop_index"):
        reconstruct_logical_history(raw)


def test_reconstruct_rejects_drop_index_on_non_output() -> None:
    raw = [*_chat_batch(), _marker(RetryCut(cut_index=11, drop_index=7))]
    with pytest.raises(ValueError, match="function_call_output"):
        reconstruct_logical_history(raw)


# ------------------------------------------------------------------
# 引擎级
# ------------------------------------------------------------------


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "fanout").mkdir(parents=True, exist_ok=True)
    (root / "fanout" / "SKILL.md").write_text(_ENTRY, encoding="utf-8")
    return root


def _lookup(calls: list[str]) -> ToolSpec:
    """记录每次执行的查询键；结果带执行序号，便于区分首跑与重跑。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        calls.append(args["key"])
        return ToolResult.ok(f"{args['key']}#{calls.count(args['key'])}")

    return ToolSpec(
        name="lookup",
        description="查询",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        handler=handler,
        parallel_safe=True,
    )


def _call(call_id: str, key: str) -> dict[str, str]:
    return {"id": call_id, "name": "lookup", "arguments": json.dumps({"key": key})}


async def _to_root_end(engine: taifeng.AgentEngine, sub_id: str) -> dict[str, Any]:
    """消费到 root turn 终结，返回 turn_rewound.data（没有则为空）。"""
    rewound: dict[str, Any] = {}
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        if ev.msg.kind == "turn_rewound":
            rewound = dict(ev.msg.data)
        if ev.msg.kind in ("turn_completed", "turn_failed") and ev.msg.data.get("is_root"):
            assert ev.msg.kind == "turn_completed"
            return rewound
    raise AssertionError("stream ended before root turn finished")


async def _engine(
    tmp_path: Path, client: SimClient, calls: list[str], session: str = "s",
    **kwargs: Any,
) -> tuple[taifeng.EnginePool, taifeng.AgentEngine]:
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path), threads_dir=tmp_path / "threads",
        model_client=client, compressors=[], extra_tools=[_lookup(calls)],
        max_parallel_tool_calls=3,
    )
    engine = await pool.get_or_create(
        session_id=session, entry_skill_id="fanout", **kwargs
    )
    return pool, engine


async def _first_run(
    tmp_path: Path, calls: list[str], *extra_turns: SimTurn,
) -> tuple[taifeng.EnginePool, taifeng.AgentEngine, SimClient]:
    client = SimClient(turns=[
        SimTurn(text="查三个", tool_calls=[
            _call("a", "ka"), _call("b", "kb"), _call("c", "kc"),
        ]),
        SimTurn(text="原收尾"),
        *extra_turns,
    ])
    pool, engine = await _engine(tmp_path, client, calls)
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    await _to_root_end(engine, sub_id)
    await wait_for_condition(lambda: engine.rewind_nodes(), message="节点表未落地")
    return pool, engine, client


def _outputs(engine: taifeng.AgentEngine) -> dict[str, str]:
    return {
        item.payload["call_id"]: item.payload["output"]
        for item in engine.history_snapshot()
        if item.kind == "function_call_output"
    }


async def test_retry_middle_call_reruns_only_that_call(tmp_path: Path) -> None:
    calls: list[str] = []
    pool, engine, client = await _first_run(tmp_path, calls, SimTurn(text="新收尾"))
    before = {i.payload["call_id"]: i.id for i in engine.history_snapshot()
              if i.kind == "function_call_output"}
    node = next(n for n in engine.rewind_nodes() if n.call_id == "b")

    sub_id = await engine.submit(Rewind(node_id=node.node_id, mode="retry_tool"))
    rewound = await _to_root_end(engine, sub_id)

    assert sorted(calls) == ["ka", "kb", "kb", "kc"]
    assert _outputs(engine) == {"a": "ka#1", "b": "kb#2", "c": "kc#1"}
    after = {i.payload["call_id"]: i.id for i in engine.history_snapshot()
             if i.kind == "function_call_output"}
    assert (after["a"], after["c"]) == (before["a"], before["c"])
    assert after["b"] != before["b"]
    history = engine.history_snapshot()
    assert _history_orphan_call_ids(list(history)) == set()
    texts = [i.payload.get("text") for i in history if i.kind == "assistant_message"]
    assert texts == ["查三个", "新收尾"]
    assert rewound["mode"] == "retry_tool"
    assert rewound["drop_index"] is not None
    assert rewound["cut_index"] > rewound["drop_index"]
    # 续推的请求里三个调用都有结果
    last = client.ledger.last_request()
    assert last is not None
    assert [last.function_call_output_text(c) for c in ("a", "b", "c")] == [
        "ka#1", "kb#2", "kc#1",
    ]
    assert client.ledger.violations == []
    await pool.close()


async def test_retry_with_new_args_rewrites_only_that_call(tmp_path: Path) -> None:
    calls: list[str] = []
    pool, engine, _ = await _first_run(tmp_path, calls, SimTurn(text="新收尾"))
    node = next(n for n in engine.rewind_nodes() if n.call_id == "a")

    sub_id = await engine.submit(
        Rewind(node_id=node.node_id, mode="retry_tool", new_args={"key": "kz"})
    )
    await _to_root_end(engine, sub_id)

    assert sorted(calls) == ["ka", "kb", "kc", "kz"]
    assert _outputs(engine) == {"a": "kz#1", "b": "kb#1", "c": "kc#1"}
    arguments = {
        i.payload["call_id"]: json.loads(i.payload["arguments"])
        for i in engine.history_snapshot() if i.kind == "function_call"
    }
    assert arguments == {"a": {"key": "kz"}, "b": {"key": "kb"}, "c": {"key": "kc"}}
    await pool.close()


async def test_cold_reload_matches_hot_history(tmp_path: Path) -> None:
    """rewind 之后另起进程冷加载：逻辑 history 与热内存逐项相同。"""
    calls: list[str] = []
    pool, engine, _ = await _first_run(tmp_path, calls, SimTurn(text="新收尾"))
    node = next(n for n in engine.rewind_nodes() if n.call_id == "b")
    sub_id = await engine.submit(Rewind(node_id=node.node_id, mode="retry_tool"))
    await _to_root_end(engine, sub_id)
    thread_id = engine.thread_id
    hot = [(i.kind, i.id) for i in engine.history_snapshot()]
    await pool.close()

    pool2, cold = await _engine(
        tmp_path, SimClient(turns=[]), [], resume_thread_id=thread_id,
    )

    assert [(i.kind, i.id) for i in cold.history_snapshot()] == hot
    assert _history_orphan_call_ids(list(cold.history_snapshot())) == set()
    await pool2.close()


async def test_retry_can_be_repeated_on_the_same_batch(tmp_path: Path) -> None:
    """同一批次连续两次 retry（不同调用）：节点表随 history 更新，仍然自洽。"""
    calls: list[str] = []
    pool, engine, _ = await _first_run(
        tmp_path, calls, SimTurn(text="收尾二"), SimTurn(text="收尾三"),
    )
    for call_id in ("c", "a"):
        node = next(n for n in engine.rewind_nodes() if n.call_id == call_id)
        sub_id = await engine.submit(Rewind(node_id=node.node_id, mode="retry_tool"))
        await _to_root_end(engine, sub_id)
        await wait_for_condition(lambda: engine.rewind_nodes(), message="节点表未落地")

    assert sorted(calls) == ["ka", "ka", "kb", "kc", "kc"]
    assert _outputs(engine) == {"a": "ka#2", "b": "kb#1", "c": "kc#2"}
    assert _history_orphan_call_ids(list(engine.history_snapshot())) == set()
    await pool.close()
