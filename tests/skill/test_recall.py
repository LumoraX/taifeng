"""skill/recall.py 单元测试：召回数据契约形状 + 协议结构兼容。

本任务（T1）只覆盖契约形状，不验证任何召回算法（KeywordSkillRecall 属 T2）。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from taifeng.loop.cancellation import CancellationToken
from taifeng.skill.recall import RecallEntry, SkillCandidate, SkillRecall


def test_skill_candidate_is_frozen() -> None:
    """SkillCandidate 为不可变 frozen dataclass：score / confidence 分离落字段。"""
    cand = SkillCandidate(
        skill_id="analyzer",
        description="分析患者数据",
        score=3.5,
        confidence=0.87,
        matched_snippet="分析",
    )
    # 字段落地正确，且 score / confidence 是两个独立字段（后端原始分 vs 归一化置信）
    assert cand.skill_id == "analyzer"
    assert cand.score == 3.5
    assert cand.confidence == 0.87
    assert cand.matched_snippet == "分析"
    # matched_snippet 可为 None（无命中片段时）
    assert SkillCandidate(
        skill_id="x", description="d", score=0.0, confidence=0.0, matched_snippet=None
    ).matched_snippet is None
    # frozen：改字段必须抛 FrozenInstanceError
    try:
        cand.score = 9.9  # type: ignore[misc]
        raise AssertionError("SkillCandidate 应为 frozen")
    except dataclasses.FrozenInstanceError:
        pass


def test_recall_entry_is_frozen() -> None:
    """RecallEntry 为不可变 frozen dataclass：召回语料池里的一项。"""
    entry = RecallEntry(skill_id="analyzer", description="分析患者数据")
    assert dataclasses.is_dataclass(entry)
    assert entry.skill_id == "analyzer"
    assert entry.description == "分析患者数据"
    try:
        entry.skill_id = "other"  # type: ignore[misc]
        raise AssertionError("RecallEntry 应为 frozen")
    except dataclasses.FrozenInstanceError:
        pass


def test_skill_recall_protocol_accepts_stub() -> None:
    """一个最小 stub 实现满足 SkillRecall Protocol（structural typing）。"""

    class _StubRecall:
        """最小召回桩：纯函数，仅回填 pool 前 top_k 项作候选（不做真实排名）。"""

        async def recall(
            self,
            query: str,
            pool: Sequence[RecallEntry],
            *,
            top_k: int,
            cancel: CancellationToken,
        ) -> list[SkillCandidate]:
            """桩实现：截断 pool 到 top_k，confidence 固定 1.0。"""
            return [
                SkillCandidate(
                    skill_id=e.skill_id,
                    description=e.description,
                    score=1.0,
                    confidence=1.0,
                    matched_snippet=None,
                )
                for e in pool[:top_k]
            ]

    def _accepts(recaller: SkillRecall) -> None:
        """仅用于 mypy 角度验证 structural typing 成立。"""
        assert recaller is not None

    stub = _StubRecall()
    _accepts(stub)  # 结构兼容则 mypy 通过
    # runtime_checkable：isinstance 亦应成立
    assert isinstance(stub, SkillRecall)


async def test_skill_recall_stub_invocation() -> None:
    """桩实现可被实际 await 调用，返回 skill_id ⊆ pool 且 len ≤ top_k。"""

    class _StubRecall:
        async def recall(
            self,
            query: str,
            pool: Sequence[RecallEntry],
            *,
            top_k: int,
            cancel: CancellationToken,
        ) -> list[SkillCandidate]:
            return [
                SkillCandidate(
                    skill_id=e.skill_id,
                    description=e.description,
                    score=1.0,
                    confidence=1.0,
                    matched_snippet=None,
                )
                for e in pool[:top_k]
            ]

    pool = [
        RecallEntry(skill_id="a", description="da"),
        RecallEntry(skill_id="b", description="db"),
        RecallEntry(skill_id="c", description="dc"),
    ]
    cancel = CancellationToken(name="test")
    result = await _StubRecall().recall("q", pool, top_k=2, cancel=cancel)
    assert len(result) <= 2
    pool_ids = {e.skill_id for e in pool}
    assert all(c.skill_id in pool_ids for c in result)
    assert all(0.0 <= c.confidence <= 1.0 for c in result)


def test_public_import() -> None:
    """import 通：契约三件套从 taifeng.skill.recall 可导入。"""
    from taifeng.skill.recall import (  # noqa: PLC0415
        RecallEntry,
        SkillCandidate,
        SkillRecall,
    )

    assert RecallEntry is not None
    assert SkillCandidate is not None
    assert SkillRecall is not None
