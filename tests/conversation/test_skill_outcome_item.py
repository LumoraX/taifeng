"""skill_outcome ItemKind + builder：构造形状 + 对 LLM 不可见。"""
from __future__ import annotations

from taifeng.conversation.models import skill_outcome_item
from taifeng.loop.prompt import history_to_api_messages


def test_skill_outcome_item_builder():
    """builder 产出 kind=skill_outcome 的 ResponseItem，payload 即记录 payload。"""
    item = skill_outcome_item({"skill_id": "analyzer", "outcome": "success"}, thread_id="th_1")
    assert item.kind == "skill_outcome"
    assert item.thread_id == "th_1"
    assert item.payload["outcome"] == "success"


def test_skill_outcome_item_invisible_to_llm():
    """旁路记账：skill_outcome item 不得进入 LLM 消息序列（R2 中性 + 不污染上下文）。"""
    item = skill_outcome_item({"skill_id": "x", "outcome": "failure"}, thread_id="th_1")
    msgs = history_to_api_messages([item])
    assert msgs == []
