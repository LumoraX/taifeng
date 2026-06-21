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
    body: str = "",
) -> SkillDefinition:
    """造一个 SkillDefinition stub（test minimal）。

    Args:
        body: 完整 SKILL.md 正文，验证阶段经 get_body 取用，默认空串。
    """
    return SkillDefinition(
        id=sid,
        name=sid,
        description=description or f"skill {sid}",
        version="1",
        body=body,
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
    current_task: str | None = None,
) -> ToolContext:
    extras: dict[str, Any] = {
        "skill_snapshot": snapshot,
        "current_skill": caller,
    }
    if dispatcher is not None:
        extras["dispatcher"] = dispatcher
    if capabilities is not None:
        extras["capabilities"] = capabilities
    # 验证门需「原始任务」判输入适配；TurnRunner 实际会注入 current_task。
    # None 时不注入，模拟缺失场景（handler 应回退到 query）。
    if current_task is not None:
        extras["current_task"] = current_task
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


# ====================================================================
# 验证段测试 fixtures（假 recall + 假 verifier，脱离真实 LLM）
# ====================================================================


class _StubRecall:
    """返回固定候选列表的召回 stub —— 把验证段输入钉死，便于断言路由。"""

    def __init__(self, candidates: list[SkillCandidate]) -> None:
        self._candidates = candidates

    async def recall(
        self,
        query: str,
        pool: Any,
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        return list(self._candidates)


class _StubVerifier:
    """把指定 skill_id 标 applicable 的验证 stub；记录收到的 body 供断言。"""

    def __init__(self, applicable_ids: set[str]) -> None:
        self._applicable_ids = applicable_ids
        # 记录 verify 实际收到的 (skill_id -> body)，供 get_body 取道断言
        self.seen_bodies: dict[str, str | None] = {}
        # 记录 verify 实际收到的 task（断言喂的是 current_task 而非关键词 query）
        self.seen_task: str | None = None

    async def verify(
        self,
        task: str,
        candidates: Any,
        *,
        get_body: Any,
        cancel: CancellationToken,
    ) -> list[Any]:
        from taifeng.skill.verify import VerifiedCandidate

        self.seen_task = task
        results: list[VerifiedCandidate] = []
        for c in candidates:
            self.seen_bodies[c.skill_id] = get_body(c.skill_id)
            if c.skill_id in self._applicable_ids:
                results.append(
                    VerifiedCandidate(
                        skill_id=c.skill_id,
                        description=c.description,
                        recall_confidence=c.confidence,
                        applicable=True,
                        # 验证置信故意与召回置信不同，便于断言键名取的是 verify 值
                        verify_confidence=0.91,
                        reason=f"{c.skill_id} 输入要求满足",
                    )
                )
        return results


def _mk_candidate(sid: str, *, confidence: float = 0.5) -> SkillCandidate:
    """造一条召回候选 stub（recall 阶段置信，与 verify 置信区分）。"""
    return SkillCandidate(
        skill_id=sid,
        description=f"skill {sid} 描述",
        score=confidence * 10,
        confidence=confidence,
        matched_snippet="片段",
    )


# ====================================================================
# 7. 启用验证：只返回 applicable 的、键名 confidence=verify_confidence、含 reason
# ====================================================================


@pytest.mark.asyncio
async def test_verify_returns_only_applicable_with_verify_confidence() -> None:
    """注入假 recall（2 候选）+ 假 verifier（标 1 个 applicable）→ 只返回该候选，
    confidence 键值=verify_confidence、带 reason、不带 matched_snippet。"""
    children = {
        "good": _mk_skill("good", description="可用技能", body="正文 good"),
        "bad": _mk_skill("bad", description="不适用技能", body="正文 bad"),
    }
    caller = _mk_skill("entry", child=frozenset(children), entry=True)
    snap = _make_snapshot([caller, *children.values()])
    recall = _StubRecall([_mk_candidate("good"), _mk_candidate("bad")])
    verifier = _StubVerifier(applicable_ids={"good"})
    tool = make_search_skills_tool(
        recall, default_top_k=5, max_top_k=20, verifier=verifier
    )

    r = await tool.handler(
        {"query": "找可用技能"}, _make_ctx(snapshot=snap, caller=caller)
    )
    assert not r.is_error
    payload = json.loads(r.output)
    assert isinstance(payload, list)
    assert len(payload) == 1
    item = payload[0]
    assert item["skill_id"] == "good"
    # 键名仍是 confidence，但值是 verify_confidence（0.91），不是召回的 0.5
    assert item["confidence"] == pytest.approx(0.91)
    assert item["reason"] == "good 输入要求满足"
    # 启用验证后不再外露 matched_snippet
    assert "matched_snippet" not in item


# ====================================================================
# 7b. 验证器拿到的 task = ctx.extras['current_task']（原始任务），不是关键词 query
#     （召回要为匹配优化的关键词 query，验证要含输入上下文的原始任务——不能共用）
# ====================================================================


@pytest.mark.asyncio
async def test_verify_receives_current_task_not_query() -> None:
    """注入 current_task 时，verifier.verify 的 task 实参 = current_task（非 query）。

    根因回归（详情五）：search_skills 曾把关键词 query 喂给验证器，剥离了「附件是
    照片」等输入上下文导致验证误拒。此用例钉死：验证拿的是原始任务。
    """
    child = _mk_skill("good", description="可用技能", body="正文 good")
    caller = _mk_skill("entry", child=frozenset({"good"}), entry=True)
    snap = _make_snapshot([caller, child])
    recall = _StubRecall([_mk_candidate("good")])
    verifier = _StubVerifier(applicable_ids={"good"})
    tool = make_search_skills_tool(
        recall, default_top_k=5, max_top_k=20, verifier=verifier
    )

    original_task = "附件是一张发票照片，帮我把上面的金额提取出来"
    r = await tool.handler(
        {"query": "图片 OCR 文字识别"},  # 关键词 query（剥离了输入上下文）
        _make_ctx(snapshot=snap, caller=caller, current_task=original_task),
    )
    assert not r.is_error
    # 验证器必须拿到原始任务（含「附件是照片」），不是被剥离的关键词 query
    assert verifier.seen_task == original_task


@pytest.mark.asyncio
async def test_verify_falls_back_to_query_when_no_current_task() -> None:
    """缺 current_task（ctx 未注入）时，verifier.verify 的 task 回退为 query。

    保证旧路径（无 current_task 注入的裸调用 / 老上下文）不崩，回退到 query。
    """
    child = _mk_skill("good", description="可用技能", body="正文 good")
    caller = _mk_skill("entry", child=frozenset({"good"}), entry=True)
    snap = _make_snapshot([caller, child])
    recall = _StubRecall([_mk_candidate("good")])
    verifier = _StubVerifier(applicable_ids={"good"})
    tool = make_search_skills_tool(
        recall, default_top_k=5, max_top_k=20, verifier=verifier
    )

    r = await tool.handler(
        {"query": "图片 OCR 文字识别"},
        _make_ctx(snapshot=snap, caller=caller),  # 不注入 current_task
    )
    assert not r.is_error
    assert verifier.seen_task == "图片 OCR 文字识别"


# ====================================================================
# 8. 全不 applicable / 召回空 → 显式 no_match 信号
# ====================================================================


@pytest.mark.asyncio
async def test_verify_all_inapplicable_returns_no_match() -> None:
    """召回有候选但验证全不适用 → 返回 {"no_match": true, "hint": ...} 显式信号。"""
    child = _mk_skill("c1", description="技能", body="正文 c1")
    caller = _mk_skill("entry", child=frozenset({"c1"}), entry=True)
    snap = _make_snapshot([caller, child])
    recall = _StubRecall([_mk_candidate("c1")])
    verifier = _StubVerifier(applicable_ids=set())  # 全不适用
    tool = make_search_skills_tool(
        recall, default_top_k=5, max_top_k=20, verifier=verifier
    )

    r = await tool.handler(
        {"query": "找技能"}, _make_ctx(snapshot=snap, caller=caller)
    )
    payload = json.loads(r.output)
    assert isinstance(payload, dict)
    assert payload["no_match"] is True
    assert isinstance(payload["hint"], str) and payload["hint"]


@pytest.mark.asyncio
async def test_verify_empty_recall_returns_no_match() -> None:
    """召回本就空 → 同样返回显式 no_match 信号（不返回空数组伪装）。"""
    child = _mk_skill("c1", description="技能", body="正文 c1")
    caller = _mk_skill("entry", child=frozenset({"c1"}), entry=True)
    snap = _make_snapshot([caller, child])
    recall = _StubRecall([])  # 召回空
    verifier = _StubVerifier(applicable_ids=set())
    tool = make_search_skills_tool(
        recall, default_top_k=5, max_top_k=20, verifier=verifier
    )

    r = await tool.handler(
        {"query": "找技能"}, _make_ctx(snapshot=snap, caller=caller)
    )
    payload = json.loads(r.output)
    assert payload["no_match"] is True


# ====================================================================
# 9. emit skill_candidates_verified（verified_count / dropped_count）
# ====================================================================


@pytest.mark.asyncio
async def test_emits_verified_event() -> None:
    """启用验证时发射 skill_candidates_verified，带 verified/dropped 计数。"""
    children = {
        "good": _mk_skill("good", description="可用", body="正文 good"),
        "bad": _mk_skill("bad", description="不适用", body="正文 bad"),
    }
    caller = _mk_skill("entry", child=frozenset(children), entry=True)
    snap = _make_snapshot([caller, *children.values()])
    recall = _StubRecall([_mk_candidate("good"), _mk_candidate("bad")])
    verifier = _StubVerifier(applicable_ids={"good"})
    disp = _FakeDispatcher()
    tool = make_search_skills_tool(
        recall, default_top_k=5, max_top_k=20, verifier=verifier
    )

    await tool.handler(
        {"query": "找可用"},
        _make_ctx(snapshot=snap, caller=caller, dispatcher=disp),
    )
    kinds = disp.kinds()
    assert "skill_candidates_verified" in kinds
    verified_ev = next(
        e.msg for e in disp.emitted if e.msg.kind == "skill_candidates_verified"
    )
    # 召回 2、验证通过 1 → dropped=1
    assert verified_ev.data["verified_count"] == 1
    assert verified_ev.data["dropped_count"] == 1


# ====================================================================
# 10. verifier is None → 保持现状（返回召回候选，不验证）
# ====================================================================


@pytest.mark.asyncio
async def test_verifier_none_keeps_recall_shape() -> None:
    """不注入 verifier → 返回召回候选（含 matched_snippet，confidence=召回置信）。"""
    child = _mk_skill("c1", description="技能", body="正文 c1")
    caller = _mk_skill("entry", child=frozenset({"c1"}), entry=True)
    snap = _make_snapshot([caller, child])
    recall = _StubRecall([_mk_candidate("c1", confidence=0.5)])
    tool = make_search_skills_tool(recall, default_top_k=5, max_top_k=20)

    r = await tool.handler(
        {"query": "找技能"}, _make_ctx(snapshot=snap, caller=caller)
    )
    payload = json.loads(r.output)
    assert len(payload) == 1
    item = payload[0]
    # 未验证：保留召回置信 + matched_snippet，不带 reason
    assert item["confidence"] == pytest.approx(0.5)
    assert "matched_snippet" in item
    assert "reason" not in item


# ====================================================================
# 11. get_body 经 snapshot 取到完整 body
# ====================================================================


@pytest.mark.asyncio
async def test_get_body_from_snapshot() -> None:
    """verifier 经 get_body 收到的 body 是 snapshot 里定义的完整 body。"""
    child = _mk_skill("c1", description="技能", body="完整 SKILL.md 正文 c1")
    caller = _mk_skill("entry", child=frozenset({"c1"}), entry=True)
    snap = _make_snapshot([caller, child])
    recall = _StubRecall([_mk_candidate("c1")])
    verifier = _StubVerifier(applicable_ids={"c1"})
    tool = make_search_skills_tool(
        recall, default_top_k=5, max_top_k=20, verifier=verifier
    )

    await tool.handler(
        {"query": "找技能"}, _make_ctx(snapshot=snap, caller=caller)
    )
    # verifier 应通过 get_body 取到 snapshot 里 c1 的完整 body
    assert verifier.seen_bodies["c1"] == "完整 SKILL.md 正文 c1"


# ====================================================================
# 12. cancel 在 recall 与 verify 之间传播
# ====================================================================


class _CancelBetweenRecall:
    """recall 返回候选前先取消 token —— 让 recall 与 verify 之间的 check 命中。"""

    def __init__(self, candidates: list[SkillCandidate], cancel: CancellationToken) -> None:
        self._candidates = candidates
        self._cancel = cancel

    async def recall(
        self,
        query: str,
        pool: Any,
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        # 模拟 recall 成功返回，但随后链路被取消（如用户中断）
        self._cancel.cancel()
        return list(self._candidates)


class _NeverCalledVerifier:
    """若被调用即断言失败 —— 验证 cancel 在 recall 与 verify 间已拦住。"""

    async def verify(
        self,
        task: str,
        candidates: Any,
        *,
        get_body: Any,
        cancel: CancellationToken,
    ) -> list[Any]:
        raise AssertionError("cancel 后不应进入 verify")


@pytest.mark.asyncio
async def test_cancel_between_recall_and_verify() -> None:
    """recall 后 token 已取消 → 在 verify 前抛 CancelledError，verifier 不被调用。"""
    import asyncio

    child = _mk_skill("c1", description="技能", body="正文 c1")
    caller = _mk_skill("entry", child=frozenset({"c1"}), entry=True)
    snap = _make_snapshot([caller, child])
    cancel = CancellationToken()
    recall = _CancelBetweenRecall([_mk_candidate("c1")], cancel)
    tool = make_search_skills_tool(
        recall,
        default_top_k=5,
        max_top_k=20,
        verifier=_NeverCalledVerifier(),
    )

    with pytest.raises(asyncio.CancelledError):
        await tool.handler(
            {"query": "找技能"},
            _make_ctx(snapshot=snap, caller=caller, cancel=cancel),
        )
