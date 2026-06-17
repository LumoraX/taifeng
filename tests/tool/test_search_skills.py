"""search_skills 工具测试 —— 相位 2 deferred 召回入口（T5）。

覆盖：
    - 返回候选结构（skill_id / description / confidence / matched_snippet，无 score）
    - G4 过滤（C2）：model_invocable=False 的 child 不入召回候选
    - 两可观测事件：skill_search_invoked + skill_candidates_returned
    - top_k 超 max_top_k 被钳制
    - cancel：已取消 token 传播 CancelledError
    - 召回池来自 caller.child_skills（不是 reachable 全集）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.event import EventMsg
from taifeng.skill.definition import (
    SkillDefinition,
    SkillExposure,
    SkillRequirements,
)
from taifeng.skill.eligibility import RuntimeCapabilities
from taifeng.skill.recall import KeywordSkillRecall, RecallEntry, SkillCandidate
from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.builtins import make_search_skills_tool
from taifeng.tool.spec import ToolContext


# ====================================================================
# fixtures
# ====================================================================


def _mk_skill(
    sid: str,
    *,
    description: str = "",
    child: frozenset[str] = frozenset(),
    entry: bool = False,
    exposure: SkillExposure | None = None,
    requires: SkillRequirements | None = None,
) -> SkillDefinition:
    """造一个 SkillDefinition stub（test minimal）。"""
    return SkillDefinition(
        id=sid,
        name=sid,
        description=description or f"skill {sid}",
        version="1",
        body="",
        body_path=Path("/tmp") / sid,
        type="composite" if (child or entry) else "atomic",
        entry=entry,
        child_skills=child,
        max_call_depth=6,
        exposure=exposure or SkillExposure(),
        requires=requires or SkillRequirements(),
    )


class _FakeDispatcher:
    """模拟 TurnRunner._emit：收 msg 实例后包 EventMsg 存起来供断言。"""

    def __init__(self) -> None:
        self.emitted: list[EventMsg] = []

    async def _emit(self, msg: Any) -> None:
        self.emitted.append(EventMsg(submission_id="sub-fake", msg=msg))

    def kinds(self) -> list[str]:
        """返回已发射事件的 kind 序列。"""
        return [e.msg.kind for e in self.emitted]


def _make_snapshot(skills: list[SkillDefinition]) -> SkillSnapshot:
    """造一个所有 skill 互相可达的 snapshot（reachable 故意比 child 宽）。"""
    all_ids = frozenset(s.id for s in skills)
    reachable = {s.id: all_ids for s in skills}
    return SkillSnapshot(version=1, skills=tuple(skills), reachable_graph=reachable)


def _make_ctx(
    *,
    snapshot: SkillSnapshot,
    caller: SkillDefinition,
    dispatcher: _FakeDispatcher | None = None,
    capabilities: RuntimeCapabilities | None = None,
    cancel: CancellationToken | None = None,
) -> ToolContext:
    extras: dict[str, Any] = {
        "skill_snapshot": snapshot,
        "current_skill": caller,
    }
    if dispatcher is not None:
        extras["dispatcher"] = dispatcher
    if capabilities is not None:
        extras["capabilities"] = capabilities
    return ToolContext(
        call_id="tc-search",
        cancel=cancel or CancellationToken(),
        thread_id="t-1",
        extras=extras,
    )


# ====================================================================
# 1. 返回候选结构（无 score 键）
# ====================================================================


@pytest.mark.asyncio
async def test_returns_candidate_shape_without_score() -> None:
    """候选含 skill_id / description / confidence / matched_snippet，且不外露 score。"""
    children = {
        "sql-audit": _mk_skill("sql-audit", description="审查 SQL 拼接代码的安全性"),
        "perf-tune": _mk_skill("perf-tune", description="性能调优与基准测试"),
    }
    caller = _mk_skill("entry", child=frozenset(children), entry=True)
    snap = _make_snapshot([caller, *children.values()])
    tool = make_search_skills_tool(
        KeywordSkillRecall(), default_top_k=5, max_top_k=20
    )

    r = await tool.handler(
        {"query": "审查 SQL 安全"}, _make_ctx(snapshot=snap, caller=caller)
    )
    assert not r.is_error
    payload = json.loads(r.output)
    assert len(payload) >= 1
    top = payload[0]
    assert top["skill_id"] == "sql-audit"
    assert set(top.keys()) == {
        "skill_id",
        "description",
        "confidence",
        "matched_snippet",
    }
    # score 是内部/审计字段，绝不外露给 LLM
    assert "score" not in top


# ====================================================================
# 2. G4 过滤（C2）：model_invocable=False 不入召回候选
# ====================================================================


@pytest.mark.asyncio
async def test_g4_hidden_child_excluded() -> None:
    """exposure.model_invocable=False 的 child 不进召回池 → 不出现在候选。"""
    visible_child = _mk_skill("sql-audit", description="审查 SQL 安全")
    hidden_child = _mk_skill(
        "sql-secret",
        description="审查 SQL 安全",  # 同 query 词，确保若入池必命中
        exposure=SkillExposure(model_invocable=False),
    )
    caller = _mk_skill(
        "entry", child=frozenset({"sql-audit", "sql-secret"}), entry=True
    )
    snap = _make_snapshot([caller, visible_child, hidden_child])
    tool = make_search_skills_tool(
        KeywordSkillRecall(), default_top_k=5, max_top_k=20
    )

    r = await tool.handler(
        {"query": "审查 SQL 安全"}, _make_ctx(snapshot=snap, caller=caller)
    )
    ids = [c["skill_id"] for c in json.loads(r.output)]
    assert "sql-audit" in ids
    assert "sql-secret" not in ids


# ====================================================================
# 2b. G4a 过滤：requires 不满足的 child 不入召回（提供 capabilities 时）
# ====================================================================


@pytest.mark.asyncio
async def test_g4a_requires_filtered() -> None:
    """提供 capabilities 时，requires.bins 不满足的 child 不进召回候选。"""
    ok_child = _mk_skill("net-scan", description="扫描网络端口")
    need_bin = _mk_skill(
        "deep-scan",
        description="扫描网络深度",
        requires=SkillRequirements(bins=frozenset({"nmap"})),
    )
    caller = _mk_skill(
        "entry", child=frozenset({"net-scan", "deep-scan"}), entry=True
    )
    snap = _make_snapshot([caller, ok_child, need_bin])
    tool = make_search_skills_tool(
        KeywordSkillRecall(), default_top_k=5, max_top_k=20
    )
    # capabilities 不含 nmap → deep-scan 应被 G4a 过滤掉
    caps = RuntimeCapabilities(available_bins=frozenset())

    r = await tool.handler(
        {"query": "扫描网络"},
        _make_ctx(snapshot=snap, caller=caller, capabilities=caps),
    )
    ids = [c["skill_id"] for c in json.loads(r.output)]
    assert "net-scan" in ids
    assert "deep-scan" not in ids


# ====================================================================
# 3. 两可观测事件
# ====================================================================


@pytest.mark.asyncio
async def test_emits_both_events() -> None:
    """调用发射 skill_search_invoked + skill_candidates_returned 两事件。"""
    child = _mk_skill("sql-audit", description="审查 SQL 安全")
    caller = _mk_skill("entry", child=frozenset({"sql-audit"}), entry=True)
    snap = _make_snapshot([caller, child])
    disp = _FakeDispatcher()
    tool = make_search_skills_tool(
        KeywordSkillRecall(), default_top_k=5, max_top_k=20
    )

    await tool.handler(
        {"query": "审查 SQL 安全"},
        _make_ctx(snapshot=snap, caller=caller, dispatcher=disp),
    )
    kinds = disp.kinds()
    assert "skill_search_invoked" in kinds
    assert "skill_candidates_returned" in kinds
    # 事件 data 校验：invoked 带 query/top_k/pool_size；returned 带 count/top_ids
    invoked = next(
        e.msg for e in disp.emitted if e.msg.kind == "skill_search_invoked"
    )
    assert invoked.data["query"] == "审查 SQL 安全"
    assert invoked.data["top_k"] == 5
    assert invoked.data["pool_size"] == 1
    returned = next(
        e.msg for e in disp.emitted if e.msg.kind == "skill_candidates_returned"
    )
    assert returned.data["count"] == 1
    assert returned.data["top_ids"] == ["sql-audit"]


# ====================================================================
# 4. top_k 超 max_top_k 被钳制
# ====================================================================


@pytest.mark.asyncio
async def test_top_k_clamped_to_max() -> None:
    """top_k=99 超 max_top_k=3 → 返回数 ≤ 3，且 invoked.top_k 被钳到 3。"""
    children = [
        _mk_skill(f"c{i}", description=f"审查任务 {i} 安全") for i in range(10)
    ]
    caller = _mk_skill(
        "entry", child=frozenset(c.id for c in children), entry=True
    )
    snap = _make_snapshot([caller, *children])
    disp = _FakeDispatcher()
    tool = make_search_skills_tool(
        KeywordSkillRecall(), default_top_k=2, max_top_k=3
    )

    r = await tool.handler(
        {"query": "审查安全", "top_k": 99},
        _make_ctx(snapshot=snap, caller=caller, dispatcher=disp),
    )
    payload = json.loads(r.output)
    assert len(payload) <= 3
    invoked = next(
        e.msg for e in disp.emitted if e.msg.kind == "skill_search_invoked"
    )
    assert invoked.data["top_k"] == 3


# ====================================================================
# 5. cancel：已取消 token 传播 CancelledError
# ====================================================================


@pytest.mark.asyncio
async def test_cancelled_token_propagates() -> None:
    """已取消 token → recall 抛 CancelledError，工具向上传播。"""
    import asyncio

    child = _mk_skill("sql-audit", description="审查 SQL 安全")
    caller = _mk_skill("entry", child=frozenset({"sql-audit"}), entry=True)
    snap = _make_snapshot([caller, child])
    cancel = CancellationToken()
    cancel.cancel()
    tool = make_search_skills_tool(
        KeywordSkillRecall(), default_top_k=5, max_top_k=20
    )

    with pytest.raises(asyncio.CancelledError):
        await tool.handler(
            {"query": "审查 SQL 安全"},
            _make_ctx(snapshot=snap, caller=caller, cancel=cancel),
        )


# ====================================================================
# 6. 召回池来自 caller.child_skills（不是 reachable 全集）
# ====================================================================


class _RecordingRecall:
    """记录传入 pool 的召回后端 stub —— 用于断言池规模/内容。"""

    def __init__(self) -> None:
        self.seen_pool: list[RecallEntry] | None = None
        self.seen_top_k: int | None = None

    async def recall(
        self,
        query: str,
        pool: Any,
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        self.seen_pool = list(pool)
        self.seen_top_k = top_k
        return []


@pytest.mark.asyncio
async def test_pool_from_child_skills_not_reachable() -> None:
    """reachable 比 child_skills 宽时，召回池只含 child_skills（窄白名单）。"""
    # caller 只有 1 个 child，但 snapshot reachable 含全部 3 个 skill
    direct_child = _mk_skill("direct", description="直接子技能")
    grandchild = _mk_skill("grand", description="孙子技能")
    caller = _mk_skill("entry", child=frozenset({"direct"}), entry=True)
    snap = _make_snapshot([caller, direct_child, grandchild])
    # 确认 reachable 确实更宽（前置断言，否则本测试无意义）
    assert snap.reachable_from("entry") == frozenset(
        {"entry", "direct", "grand"}
    )

    rec = _RecordingRecall()
    tool = make_search_skills_tool(rec, default_top_k=5, max_top_k=20)
    await tool.handler(
        {"query": "技能"}, _make_ctx(snapshot=snap, caller=caller)
    )
    assert rec.seen_pool is not None
    pool_ids = {e.skill_id for e in rec.seen_pool}
    assert pool_ids == {"direct"}  # 仅 child_skills，不含 grandchild / entry 自身
