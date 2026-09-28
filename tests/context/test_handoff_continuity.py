"""compaction-continuity —— 压缩条目带续接前言 + 被压缩区间最近用户原话 + 新摘要分段。

回归点：用户原话被一并交给 LLM 转述，意图与约束最易走样；文档写了续接提示语，代码里没有。
"""

from __future__ import annotations

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionContext
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.context.strategies.handoff import HANDOFF_SYSTEM_PROMPT_ZH
from taifeng.context.strategies.handoff_continuity import (
    CONTINUATION_PREAMBLE,
    compose_handoff_summary,
    select_recent_user_messages,
)
from taifeng.conversation.models import ResponseItem, assistant_message, user_message
from taifeng.llm.providers.sim import SimClient, SimTurn

_UUID = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"


def _users(*texts: str) -> list[ResponseItem]:
    return [user_message(t, thread_id="t") for t in texts]


def test_select_keeps_most_recent_within_budget_in_order() -> None:
    """从最新往回取，超预算即停，返回按原顺序。"""
    items = [*_users("a" * 350, "b" * 350), assistant_message("回答", thread_id="t", model="m"),
             *_users("c" * 350)]
    # 每条约 100 token；预算 250 → 只放得下最近两条
    assert select_recent_user_messages(items, 250) == ["b" * 350, "c" * 350]


def test_select_truncates_oversized_newest_message() -> None:
    """最新一条单独超预算 → 截断保留头尾，而不是整条丢掉。"""
    text = "HEAD" + "x" * 10_000 + "TAIL"
    (kept,) = select_recent_user_messages(_users(text), 100)
    assert kept.startswith("HEAD") and kept.endswith("TAIL") and len(kept) < len(text)


@pytest.mark.parametrize("budget", [0, -1])
def test_select_disabled_by_zero_budget(budget: int) -> None:
    assert select_recent_user_messages(_users("hi"), budget) == []


def test_select_skips_blank_and_non_user_items() -> None:
    items = [*_users("  "), assistant_message("x", thread_id="t", model="m"), *_users("要三段")]
    assert select_recent_user_messages(items, 1000) == ["要三段"]


def test_compose_layout() -> None:
    text = compose_handoff_summary("## 进度\n- 写第二段", ["报告写三段", "不要改公共 API"])
    assert text.startswith(CONTINUATION_PREAMBLE)
    assert text.index("<recent_user_messages>") < text.index("<summary>")
    assert "<user_message>\n报告写三段\n</user_message>" in text
    assert "## 进度\n- 写第二段" in text


def test_compose_without_user_messages_has_no_section() -> None:
    assert "<recent_user_messages>" not in compose_handoff_summary("s", [])


def test_summary_prompt_asks_for_current_work_and_errors() -> None:
    assert "## 当前工作 (Current Work)" in HANDOFF_SYSTEM_PROMPT_ZH
    assert "## 错误与修复 (Errors & Fixes)" in HANDOFF_SYSTEM_PROMPT_ZH


def _ctx(items: list[ResponseItem]) -> CompressionContext:
    budget = ContextBudget(context_window=1000, soft_limit_ratio=0.85, preserve_tail_messages=2)
    return CompressionContext(
        history=items, token_estimate=int(budget.soft_limit) + 100, budget=budget,
        cache_anchor_index=-1, phase="pre_turn",
        available_injections=frozenset({InitialContextInjection.BEFORE_LAST_USER_MESSAGE}),
    )


async def test_compacted_item_carries_verbatim_user_messages() -> None:
    """压缩条目里有前言、被压缩区间的用户原话与 LLM 摘要；保留段的用户消息不重复收录。"""
    items = [
        *_users("报告写成三段，每段不超过 200 字"),
        assistant_message("好的", thread_id="t", model="m"),
        *_users("第二段要引用数据"),
        assistant_message("收到", thread_id="t", model="m"),
        *_users("继续"),
        assistant_message("写第三段", thread_id="t", model="m"),
    ]
    client = SimClient(turns=[SimTurn(text="## 进度\n- 写到第二段")])
    strategy = HandoffCompactionStrategy(model_client=client, model="m")
    result = await strategy.compress(_ctx(items), InitialContextInjection.BEFORE_LAST_USER_MESSAGE)
    assert result.success
    (compacted,) = [it for it in result.new_history if it.kind == "compacted"]
    summary = compacted.payload["summary"]
    assert summary.startswith(CONTINUATION_PREAMBLE)
    assert "报告写成三段，每段不超过 200 字" in summary and "第二段要引用数据" in summary
    assert "继续" not in summary.split("<summary>")[0]  # 尾部保留段不重复收录
    assert "## 进度\n- 写到第二段" in summary


async def test_identifier_in_user_message_needs_no_regeneration() -> None:
    """标识符在用户原话里 → 原样保留即不算丢失，摘要无需重生成。"""
    items = [*_users(f"任务 ID {_UUID} " + "数据 " * 60),
             *_users(*[f"msg-{i} " + "x" * 200 for i in range(7)])]
    client = SimClient(turns=[SimTurn(text="## 进度\n- 处理中（没写 ID）")])
    strategy = HandoffCompactionStrategy(model_client=client, model="m")
    result = await strategy.compress(_ctx(items), InitialContextInjection.BEFORE_LAST_USER_MESSAGE)
    assert result.success
    assert client._idx == 1  # noqa: SLF001 —— 只调用了一次摘要
