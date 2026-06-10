"""cache-anchor-message-index 回归:cache anchor 的 history→messages 坐标映射。

CacheBreakpoint.index 语义是 messages 下标(anthropic 据此打 cache_control),
而 cache_anchor_index 是 history 下标(压缩 anchor_preserved_until)。
history→messages 非 1:1(记账 item 跳过/同轮合并),必须映射。
"""
from __future__ import annotations

from pathlib import Path

from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    reasoning,
    user_message,
)
from taifeng.loop.prompt import build_api_request
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.registry import SkillSnapshot

TID = "t-test"


def _entry() -> SkillDefinition:
    """构造 entry-eligible composite skill(装配测试用,不实际 IO)。"""
    return SkillDefinition(
        id="e", name="e", description="测试入口", version="1.0.0",
        type="composite", entry=True, body="入口",
        body_path=Path("_test_e.md"),
        child_skills=frozenset(), tool_names=frozenset(), max_call_depth=3,
    )


def _req(history: list[ResponseItem], anchor: int):
    return build_api_request(
        entry=_entry(),
        snapshot=SkillSnapshot(version=1, skills=()),
        history=history,
        tools=[],
        model="m",
        cache_anchor_index=anchor,
    )


def test_anchor_maps_past_bookkeeping_items() -> None:
    """记账 item(suspension)不产出消息:anchor 必须映射到 messages 坐标。"""
    from taifeng.conversation.models import suspension_item

    hist = [
        user_message("a", thread_id=TID),                       # h0 → m0
        suspension_item(record_id="r", submission_id="s", turn_index=1,
                   pending=[], created_at=0, thread_id=TID),    # h1 → (无产出)
        assistant_message("b", thread_id=TID, model="m"),        # h2 → m1
    ]
    req = _req(hist, anchor=3)
    assert [bp.index for bp in req.cache_breakpoints] == [1], \
        f"应打在 messages 下标 1,实得 {[bp.index for bp in req.cache_breakpoints]}"


def test_anchor_maps_with_round_merge() -> None:
    """同轮合并(reasoning+am+fc 折叠为一条):anchor 映射到合并后坐标。"""
    hist = [
        user_message("a", thread_id=TID),                                        # h0 → m0
        reasoning("想", thread_id=TID),                                          # h1 → (附着)
        assistant_message("", thread_id=TID, model="m"),                         # h2 → m1(合并窗)
        function_call(call_id="c1", name="ask", arguments="{}", thread_id=TID),  # h3 → 并入 m1
        function_call_output(call_id="c1", output="答", thread_id=TID),           # h4 → m2
    ]
    req = _req(hist, anchor=5)
    assert [bp.index for bp in req.cache_breakpoints] == [2], \
        f"应打在 messages 下标 2(tool 消息),实得 {[bp.index for bp in req.cache_breakpoints]}"


def test_anchor_zero_or_no_prefix_message_no_breakpoint() -> None:
    """anchor=0 / 前缀无产出消息:不打点。"""
    hist = [user_message("a", thread_id=TID)]
    assert _req(hist, anchor=0).cache_breakpoints == []


def test_anchor_disabled_unchanged() -> None:
    """anchor=-1(默认未启用):不打点,与既有行为一致。"""
    hist = [user_message("a", thread_id=TID)]
    assert _req(hist, anchor=-1).cache_breakpoints == []
