"""skill 发现/召回两事件单元测试（相位 2，R3 可观测）。

钉死：
- ``skill_search_invoked`` —— 搜索发起时打点，data 语义 {query, top_k, pool_size}。
- ``skill_candidates_returned`` —— 候选返回时打点，data 语义 {count, top_ids}。
两个新 kind 都必须在 MsgKind Literal union 内（漏加会触发 mypy [assignment]），
且 ConsoleSink 各有专用渲染（不落 `evt` 兜底）。
"""
from __future__ import annotations

from typing import get_args

from taifeng.loop.event import (
    EventMsg,
    MsgKind,
    SkillCandidatesReturned,
    SkillSearchInvoked,
)
from taifeng.telemetry.console import _fmt_event


def _line(msg: object) -> str:
    """构造一条 EventMsg 并取无色渲染行（仿 telemetry 现有测试）。"""
    ev = EventMsg(submission_id="sub-123456789012", msg=msg)  # type: ignore[arg-type]
    return _fmt_event(ev, color=False)


def test_skill_search_invoked_event() -> None:
    """skill_search_invoked：kind 固定 + data 字段（query/top_k/pool_size）可读。"""
    ev = SkillSearchInvoked(data={"query": "代谢分析", "top_k": 5, "pool_size": 120})
    assert ev.kind == "skill_search_invoked"
    assert ev.data["query"] == "代谢分析"
    assert ev.data["top_k"] == 5
    assert ev.data["pool_size"] == 120


def test_skill_candidates_returned_event() -> None:
    """skill_candidates_returned：kind 固定 + data 字段（count/top_ids）可读。"""
    ev = SkillCandidatesReturned(data={"count": 2, "top_ids": ["metabolic", "lung-nodule"]})
    assert ev.kind == "skill_candidates_returned"
    assert ev.data["count"] == 2
    assert ev.data["top_ids"] == ["metabolic", "lung-nodule"]


def test_both_kinds_in_msgkind_union() -> None:
    """两个新 kind 都必须登记在 MsgKind Literal union（否则 [assignment] 报错）。"""
    members = set(get_args(MsgKind))
    assert "skill_search_invoked" in members
    assert "skill_candidates_returned" in members


def test_skill_search_invoked_has_dedicated_render() -> None:
    """skill_search_invoked：专用 tag + 字段渲染，不落 evt 兜底、非 raw dump。"""
    line = _line(SkillSearchInvoked(data={"query": "代谢分析", "top_k": 5, "pool_size": 120}))
    assert " evt " not in line, f"不应落 evt 兜底：{line}"
    assert "skill_search_invoked {" not in line, "不应是 raw data dump"
    assert "代谢分析" in line
    assert "top_k=5" in line
    assert "pool=120" in line


def test_skill_candidates_returned_has_dedicated_render() -> None:
    """skill_candidates_returned：专用 tag + 字段渲染，不落 evt 兜底、非 raw dump。"""
    line = _line(
        SkillCandidatesReturned(data={"count": 2, "top_ids": ["metabolic", "lung-nodule"]})
    )
    assert " evt " not in line, f"不应落 evt 兜底：{line}"
    assert "skill_candidates_returned {" not in line, "不应是 raw data dump"
    assert "count=2" in line
    assert "metabolic" in line
