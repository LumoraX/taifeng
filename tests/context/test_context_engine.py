"""ContextEngine 的视图校验与参考实现（ADR 0093）。"""

from __future__ import annotations

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.context.engine import (
    AssembledContext,
    AssembleRequest,
    ContextEngine,
    ContextEngineError,
    TailWindowContextEngine,
    TurnUpdate,
    orphan_call_ids,
    validate_view,
)
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    system_injection,
    user_message,
)
from taifeng.loop.cancellation import CancellationToken

TID = "thr-view"


def _turn(index: int, *, with_tool: bool = False) -> list[ResponseItem]:
    """一轮：用户消息 +（可选的工具往返）+ 回答。"""
    items = [user_message(f"问题 {index}", thread_id=TID)]
    if with_tool:
        items.append(function_call(
            call_id=f"c{index}", name="lookup", arguments="{}", thread_id=TID,
        ))
        items.append(function_call_output(
            call_id=f"c{index}", output="结果", thread_id=TID,
        ))
    items.append(assistant_message(f"回答 {index}", thread_id=TID, model="m"))
    return items


def _history(turns: int, *, with_tool: bool = False) -> list[ResponseItem]:
    return [item for index in range(1, turns + 1) for item in _turn(index, with_tool=with_tool)]


def _request(history: list[ResponseItem], thread_id: str = TID) -> AssembleRequest:
    return AssembleRequest(
        thread_id=thread_id, entry_skill_id="entry", history=tuple(history),
        budget=ContextBudget(context_window=10_000), history_tokens=0,
        cache_anchor_index=-1, cancel=CancellationToken(),
    )


def _view(items: list[ResponseItem], anchor: int = -1) -> AssembledContext:
    return AssembledContext(items=items, cache_invalidated=False, anchor_preserved_until=anchor)


def _texts(items: object) -> list[str]:
    return [str(item.payload.get("text")) for item in items if "text" in item.payload]  # type: ignore[attr-defined]


# ------------------------------------------------------------------
# 结构校验
# ------------------------------------------------------------------


def test_orphans_are_detected_in_both_directions() -> None:
    history = _turn(1, with_tool=True)

    assert orphan_call_ids(history) == set()
    assert orphan_call_ids(history[:2]) == {"c1"}
    assert orphan_call_ids([history[0], history[2]]) == {"c1"}


def test_valid_view_passes() -> None:
    history = _history(3, with_tool=True)

    validate_view("e", history, _view(history[4:], anchor=0))


def test_view_may_contain_items_that_are_not_in_history() -> None:
    history = _history(2)
    recalled = system_injection(text="检索到的旧内容", thread_id=TID, source="recall")

    validate_view("e", history, _view([recalled, *history[2:]]))


def test_empty_view_is_rejected() -> None:
    with pytest.raises(ContextEngineError, match="empty view"):
        validate_view("e", _history(1), _view([]))


def test_empty_view_is_fine_for_empty_history() -> None:
    validate_view("e", [], _view([]))


@pytest.mark.parametrize("keep", [(0, 1), (0, 2, 3)])
def test_split_tool_pair_is_rejected(keep: tuple[int, ...]) -> None:
    history = _turn(1, with_tool=True)

    with pytest.raises(ContextEngineError, match=r"unpaired tool calls: \['c1'\]"):
        validate_view("e", history, _view([history[index] for index in keep]))


def test_call_dangling_in_history_may_stay_dangling() -> None:
    """history 里本来就悬空的调用（正在等结果）不算视图的错。"""
    history = _turn(1, with_tool=True)[:2]

    validate_view("e", history, _view(history))


@pytest.mark.parametrize("anchor", [-2, 4, 99])
def test_anchor_out_of_range_is_rejected(anchor: int) -> None:
    history = _history(2)

    with pytest.raises(ContextEngineError, match="anchor_preserved_until"):
        validate_view("e", history, _view(history, anchor=anchor))


# ------------------------------------------------------------------
# TailWindowContextEngine
# ------------------------------------------------------------------


async def test_short_history_is_sent_as_is() -> None:
    engine = TailWindowContextEngine(keep_last_turns=2)

    assert await engine.assemble(_request(_history(3))) is None
    assert isinstance(engine, ContextEngine)


async def test_long_history_keeps_the_start_and_the_recent_turns() -> None:
    engine = TailWindowContextEngine(keep_last_turns=2)
    history = _history(5, with_tool=True)

    assembled = await engine.assemble(_request(history))

    assert assembled is not None
    assert _texts(assembled.items) == ["问题 1", "问题 4", "回答 4", "问题 5", "回答 5"]
    validate_view(engine.name, history, assembled)
    assert assembled.detail == {"dropped": 11, "kept_turns": 2}
    # 原 history 未被改动
    assert len(history) == 20


async def test_items_before_the_first_user_message_are_kept() -> None:
    engine = TailWindowContextEngine(keep_last_turns=1)
    summary = system_injection(text="此前的摘要", thread_id=TID, source="compaction")
    history = [summary, *_history(3)]

    assembled = await engine.assemble(_request(history))

    assert assembled is not None
    assert _texts(assembled.items) == ["此前的摘要", "问题 1", "问题 3", "回答 3"]


async def test_cache_is_invalidated_only_when_the_window_moves() -> None:
    engine = TailWindowContextEngine(keep_last_turns=1)
    history = _history(3)

    first = await engine.assemble(_request(history))
    again = await engine.assemble(_request([*history, *_turn(3)[1:]]))
    moved = await engine.assemble(_request(_history(4)))

    assert first is not None and again is not None and moved is not None
    assert (first.cache_invalidated, first.anchor_preserved_until) == (True, 0)
    # 窗口起点没动：整个视图都是稳定前缀
    assert again.cache_invalidated is False
    assert again.anchor_preserved_until == len(again.items) - 1
    assert (moved.cache_invalidated, moved.anchor_preserved_until) == (True, 0)


async def test_window_is_tracked_per_thread() -> None:
    engine = TailWindowContextEngine(keep_last_turns=1)
    history = _history(3)

    await engine.assemble(_request(history, "thr-a"))
    other = await engine.assemble(_request(history, "thr-b"))

    assert other is not None
    assert other.cache_invalidated is True


async def test_assemble_honours_cancellation() -> None:
    cancel = CancellationToken()
    cancel.cancel()
    request = AssembleRequest(
        thread_id=TID, entry_skill_id="entry", history=tuple(_history(4)),
        budget=ContextBudget(context_window=10_000), history_tokens=0,
        cache_anchor_index=-1, cancel=cancel,
    )

    with pytest.raises(BaseException) as raised:  # noqa: PT011
        await TailWindowContextEngine(keep_last_turns=1).assemble(request)

    assert "cancel" in type(raised.value).__name__.lower()


async def test_after_turn_is_a_no_op() -> None:
    engine = TailWindowContextEngine(keep_last_turns=1)

    await engine.after_turn(TurnUpdate(
        thread_id=TID, entry_skill_id="entry", new_items=(), history=(),
    ))


def test_keep_last_turns_is_validated() -> None:
    with pytest.raises(ValueError, match="keep_last_turns"):
        TailWindowContextEngine(keep_last_turns=0)
