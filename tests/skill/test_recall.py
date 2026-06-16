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


# ----------------------------------------------------------------------------
# T2：KeywordSkillRecall 内核默认召回后端（零依赖、确定性、idf + 长度归一）
# ----------------------------------------------------------------------------

import pytest  # noqa: E402, PLC0415

from taifeng.skill.recall import KeywordSkillRecall  # noqa: E402


def _cn_pool() -> list[RecallEntry]:
    """构造一组中文 RecallEntry 语料池（覆盖差异化主题，便于区分相关度）。"""
    return [
        RecallEntry(skill_id="finance", description="处理报销纠纷与财务审计的专家"),
        RecallEntry(skill_id="weather", description="查询天气预报与气象数据"),
        RecallEntry(skill_id="travel", description="规划旅行路线与酒店预订"),
        RecallEntry(skill_id="code", description="审查代码质量与重构建议"),
    ]


async def test_keyword_recall_relevant_first() -> None:
    """与某条强相关的 query → 该条 confidence 最高且排第一。"""
    recall = KeywordSkillRecall()
    pool = _cn_pool()
    cancel = CancellationToken(name="t")
    # query 命中 finance 的「报销纠纷」「财务」
    result = await recall.recall("报销纠纷怎么处理财务问题", pool, top_k=4, cancel=cancel)
    assert result, "应有候选被召回"
    assert result[0].skill_id == "finance"
    # 最高分那条 confidence 归一到 1.0
    assert result[0].confidence == pytest.approx(1.0)


async def test_keyword_recall_confidence_in_range() -> None:
    """所有候选 confidence ∈ [0,1]，最高分恰为 1.0。"""
    recall = KeywordSkillRecall()
    pool = _cn_pool()
    result = await recall.recall(
        "旅行路线规划", pool, top_k=4, cancel=CancellationToken(name="t")
    )
    assert result
    assert all(0.0 <= c.confidence <= 1.0 for c in result)
    assert max(c.confidence for c in result) == pytest.approx(1.0)


async def test_keyword_recall_top_k_truncation() -> None:
    """池命中数大于 top_k 时只返回 top_k 条。"""
    recall = KeywordSkillRecall()
    # 四条 description 都含「数据」，query 用「数据」命中全部
    pool = [
        RecallEntry(skill_id="a", description="数据分析能力"),
        RecallEntry(skill_id="b", description="数据采集能力"),
        RecallEntry(skill_id="c", description="数据清洗能力"),
        RecallEntry(skill_id="d", description="数据建模能力"),
    ]
    result = await recall.recall(
        "数据", pool, top_k=2, cancel=CancellationToken(name="t")
    )
    assert len(result) == 2


async def test_keyword_recall_empty_pool() -> None:
    """空池 → 返回空列表。"""
    recall = KeywordSkillRecall()
    result = await recall.recall(
        "任意 query", [], top_k=5, cancel=CancellationToken(name="t")
    )
    assert result == []


async def test_keyword_recall_all_zero_returns_empty() -> None:
    """C7：非空池但全 0 匹配（query 与所有 description 毫不相关）→ 返回空列表。"""
    recall = KeywordSkillRecall()
    pool = _cn_pool()
    # query 是与池内全部主题无关的内容
    result = await recall.recall(
        "xyzzy 不存在词 qwerty", pool, top_k=4, cancel=CancellationToken(name="t")
    )
    assert result == []


async def test_keyword_recall_stable_tie_ordering() -> None:
    """C8：两条对称、score 相等的候选 → 按 skill_id 升序定序（确定性）。"""
    recall = KeywordSkillRecall()
    # zzz 与 aaa 描述完全对称（同词集），命中 score 相等
    pool = [
        RecallEntry(skill_id="zzz", description="报销 审计"),
        RecallEntry(skill_id="aaa", description="报销 审计"),
    ]
    result = await recall.recall(
        "报销 审计", pool, top_k=2, cancel=CancellationToken(name="t")
    )
    assert len(result) == 2
    # 同 confidence → skill_id 升序：aaa 在前
    assert result[0].confidence == pytest.approx(result[1].confidence)
    assert [c.skill_id for c in result] == ["aaa", "zzz"]


async def test_keyword_recall_cjk_hit() -> None:
    """CJK：中文 query 命中中文 description。"""
    recall = KeywordSkillRecall()
    pool = _cn_pool()
    result = await recall.recall(
        "天气预报", pool, top_k=4, cancel=CancellationToken(name="t")
    )
    assert result
    assert result[0].skill_id == "weather"
    assert result[0].matched_snippet is not None


async def test_keyword_recall_ascii_hit() -> None:
    """ASCII：英文 query 命中英文 description。"""
    recall = KeywordSkillRecall()
    pool = [
        RecallEntry(skill_id="search", description="full text search and indexing engine"),
        RecallEntry(skill_id="mail", description="send and receive email messages"),
    ]
    result = await recall.recall(
        "text search engine", pool, top_k=2, cancel=CancellationToken(name="t")
    )
    assert result
    assert result[0].skill_id == "search"
    assert result[0].matched_snippet is not None


async def test_keyword_recall_deterministic() -> None:
    """确定性：同输入连调两次，结果列表完全相等。"""
    recall = KeywordSkillRecall()
    pool = _cn_pool()
    args = ("报销 天气 旅行 代码", pool)
    r1 = await recall.recall(*args, top_k=4, cancel=CancellationToken(name="t"))
    r2 = await recall.recall(*args, top_k=4, cancel=CancellationToken(name="t"))
    assert r1 == r2


async def test_keyword_recall_cancel_raises() -> None:
    """cancel：传入已取消 token → 抛 asyncio.CancelledError（与 raise_if_cancelled 一致）。"""
    import asyncio  # noqa: PLC0415

    recall = KeywordSkillRecall()
    pool = _cn_pool()
    cancel = CancellationToken(name="t")
    cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recall.recall("报销纠纷", pool, top_k=4, cancel=cancel)
