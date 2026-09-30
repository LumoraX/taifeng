"""选择置信度分流门端到端测试（认知回路相位 3，skill-selection-gate，ADR 0088）。

经 ``EnginePool.create(selection_gate=...)`` 启用后：

- ``search_skills`` 的每个候选带 ``route``；全部低置信时返回显式 ``no_match``；
- ``call_skill`` 派发经发现选中的 skill 前过分流门：``proceed`` 放行、``trial`` 要求先试用、
  ``escalate`` 拦下；
- 作者白名单里直接选中的 skill、以及未启用分流门的 pool 行为不变。

召回后端用确定性 stub 按 skill id 给固定置信度，断言与召回算法解耦。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.skill.recall import SkillCandidate
from taifeng.skill.selection import (
    SkillSelectionGate,
    ThresholdSelectionPolicy,
    TrialVerdict,
)
from taifeng.tool.builtins.spawn_skill import make_spawn_skill_tool
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.recall import RecallEntry

GUARD_TIMEOUT_SECONDS = 10.0
_USAGE = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)

_ENTRY_BODY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [leaf, other]
tool_names: [spawn_skill]
max_call_depth: 6
exposure:
  child_recall: deferred
---
# entry body
"""

_CHILD_BODY = """---
name: {name}
description: {name} 处理具体子任务
version: 1.0.0
type: atomic
---
# {name} body
"""


def _build_skills(tmp_path: Path) -> Path:
    """构造 entry → leaf / other 的 skill 目录。"""
    skills = tmp_path / "skills"
    (skills / "entry").mkdir(parents=True)
    (skills / "entry" / "SKILL.md").write_text(_ENTRY_BODY, encoding="utf-8")
    for name in ("leaf", "other"):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(
            _CHILD_BODY.format(name=name), encoding="utf-8"
        )
    return skills


class _ScoredRecall:
    """按 skill id 给固定置信度的召回后端；未列出的 skill 不返回。"""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    async def recall(
        self,
        query: str,
        pool: Sequence[RecallEntry],
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        """返回池内有分数的候选，按置信度降序。"""
        found = [
            SkillCandidate(
                skill_id=entry.skill_id,
                description=entry.description,
                score=self._scores[entry.skill_id],
                confidence=self._scores[entry.skill_id],
                matched_snippet=None,
            )
            for entry in pool
            if entry.skill_id in self._scores
        ]
        found.sort(key=lambda candidate: candidate.confidence, reverse=True)
        return found[:top_k]


class _FixedJudge:
    """恒给同一结论的试用门，并记录被问到的 skill。"""

    def __init__(self, approved: bool) -> None:
        self._approved = approved
        self.asked: list[tuple[str, str]] = []

    async def judge(
        self,
        *,
        task: str,
        skill_id: str,
        description: str,
        body: str,
        cancel: CancellationToken,
    ) -> TrialVerdict:
        """记录入参并返回固定结论。"""
        self.asked.append((task, skill_id))
        return TrialVerdict(approved=self._approved, reason="stub verdict")


def _search(call_id: str = "tc_search") -> SimTurn:
    """模型调一次 search_skills。"""
    return SimTurn(
        text="先搜索",
        tool_calls=[{
            "id": call_id, "name": "search_skills", "arguments": '{"query": "子任务"}',
        }],
        usage=_USAGE,
    )


def _call(skill_id: str, call_id: str) -> SimTurn:
    """模型调一次 call_skill。"""
    arguments = json.dumps({"reason": "测试派发", "skill_id": skill_id, "args": {"q": "y"}})
    return SimTurn(
        text=f"派发 {skill_id}",
        tool_calls=[{"id": call_id, "name": "call_skill", "arguments": arguments}],
        usage=_USAGE,
    )


def _read(skill_id: str, call_id: str) -> SimTurn:
    """模型调一次 read_skill。"""
    return SimTurn(
        text=f"读 {skill_id}",
        tool_calls=[{
            "id": call_id,
            "name": "read_skill",
            "arguments": json.dumps({"skill_id": skill_id}),
        }],
        usage=_USAGE,
    )


def _spawn(skill_id: str, call_id: str) -> SimTurn:
    """模型调一次 spawn_skill（分离派发）。"""
    arguments = json.dumps({"reason": "并发处理", "skill_id": skill_id, "args": {}})
    return SimTurn(
        text=f"分离派发 {skill_id}",
        tool_calls=[{"id": call_id, "name": "spawn_skill", "arguments": arguments}],
        usage=_USAGE,
    )


def _say(text: str) -> SimTurn:
    """模型只回文本。"""
    return SimTurn(text=text, usage=_USAGE)


async def _run(
    tmp_path: Path,
    turns: list[SimTurn],
    *,
    scores: dict[str, float],
    gate: SkillSelectionGate | None,
) -> tuple[SimClient, list[Any]]:
    """跑一条用户消息直到最外层 turn 结束，返回 (client, 事件列表)。"""
    client = SimClient(turns=turns)
    pool = await taifeng.EnginePool.create(
        skills_dir=_build_skills(tmp_path),
        threads_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        skill_recall=_ScoredRecall(scores),
        selection_gate=gate,
        extra_tools=[make_spawn_skill_tool(selection_gate=gate)],
    )
    try:
        engine = await pool.get_or_create(session_id="s-gate", entry_skill_id="entry")
        events = await _drain(engine, taifeng.UserMessage(text="请处理这个子任务"))
        # 分离派发的子 turn 在后台跑：等它收尾再关 pool
        await wait_for_condition(
            lambda: engine._spawn_registry.snapshot()["active"] == 0  # noqa: SLF001
        )
    finally:
        await pool.close()
    return client, events


async def _drain(engine: taifeng.AgentEngine, message: taifeng.UserMessage) -> list[Any]:
    """提交消息并收集事件，直到嵌套深度回到 0 的 turn 终结。"""
    holder: list[str] = []
    events: list[Any] = []
    done = asyncio.Event()

    async def collector() -> None:
        depth = 0
        async for event in engine.subscribe_all():
            if not holder or event.submission_id != holder[0]:
                continue
            events.append(event)
            if event.msg.kind == "skill_dispatched":
                depth += 1
            elif event.msg.kind == "skill_returned":
                depth -= 1
            if event.msg.kind in ("turn_completed", "turn_failed") and depth == 0:
                done.set()
                return

    task = asyncio.create_task(collector())
    await asyncio.sleep(0)
    holder.append(await engine.submit(message))
    try:
        await asyncio.wait_for(done.wait(), timeout=GUARD_TIMEOUT_SECONDS)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return events


def _data(events: list[Any], kind: str) -> list[dict[str, Any]]:
    """取某类事件的 data 列表。"""
    return [event.msg.data for event in events if event.msg.kind == kind]


def _kinds(events: list[Any]) -> list[str]:
    """事件 kind 序列（断言失败时的诊断信息）。"""
    return [event.msg.kind for event in events]


def _gate(judge: _FixedJudge | None = None) -> SkillSelectionGate:
    """默认阈值（0.75 / 0.4 / 间距 0.05）的分流门。"""
    return SkillSelectionGate(policy=ThresholdSelectionPolicy(), trial_judge=judge)


# ====================================================================
# proceed：高置信直接派发
# ====================================================================


async def test_high_confidence_candidate_dispatches(tmp_path: Path) -> None:
    """置信 0.9 的候选标 proceed，call_skill 直接放行。"""
    client, events = await _run(
        tmp_path,
        [_search(), _call("leaf", "tc_leaf"), _say("leaf 完成"), _say("总结")],
        scores={"leaf": 0.9},
        gate=_gate(),
    )

    found = json.loads(client.ledger.function_call_output_text("tc_search") or "")
    assert [(entry["skill_id"], entry["route"]) for entry in found] == [("leaf", "proceed")]
    assert _data(events, "skill_selection_routed") == [
        {"proceed": 1, "trial": 0, "escalate": 0, "routes": {"leaf": "proceed"}}
    ]
    gated = _data(events, "skill_selection_gated")
    assert len(gated) == 1, _kinds(events)
    assert gated[0]["admitted"] is True
    assert gated[0]["basis"] == "route_proceed"
    assert gated[0]["confidence"] == 0.9
    assert len(_data(events, "skill_dispatched")) == 1


# ====================================================================
# trial：先试用再派发
# ====================================================================


async def test_trial_candidate_needs_read_before_dispatch(tmp_path: Path) -> None:
    """置信 0.6：直接派发被拦，读过说明书后再派发放行。"""
    client, events = await _run(
        tmp_path,
        [
            _search(),
            _call("leaf", "tc_blocked"),
            _read("leaf", "tc_read"),
            _call("leaf", "tc_leaf"),
            _say("leaf 完成"),
            _say("总结"),
        ],
        scores={"leaf": 0.6},
        gate=_gate(),
    )

    blocked = client.ledger.function_call_output_text("tc_blocked") or ""
    assert "selection_needs_trial" in blocked
    assert "read_skill" in blocked
    gated = _data(events, "skill_selection_gated")
    assert [(item["call_id"], item["admitted"], item["basis"]) for item in gated] == [
        ("tc_blocked", False, "needs_trial"),
        ("tc_leaf", True, "read_skill"),
    ], _kinds(events)
    assert all(item["route"] == "trial" for item in gated)
    # 被拦的那次没有派发出子 turn
    assert len(_data(events, "skill_dispatched")) == 1


async def test_read_before_search_does_not_count_as_trial(tmp_path: Path) -> None:
    """说明书读在召回之前不算试用：试用是对这次召回结果的核实。"""
    client, events = await _run(
        tmp_path,
        [
            _read("leaf", "tc_read"),
            _search(),
            _call("leaf", "tc_blocked"),
            _say("放弃"),
        ],
        scores={"leaf": 0.6},
        gate=_gate(),
    )

    assert "selection_needs_trial" in (
        client.ledger.function_call_output_text("tc_blocked") or ""
    )
    assert _data(events, "skill_dispatched") == []


async def test_trial_judge_approval_dispatches(tmp_path: Path) -> None:
    """配置了试用门且放行：trial 档无需模型先读说明书。"""
    judge = _FixedJudge(approved=True)
    _client, events = await _run(
        tmp_path,
        [_search(), _call("leaf", "tc_leaf"), _say("leaf 完成"), _say("总结")],
        scores={"leaf": 0.6},
        gate=_gate(judge),
    )

    gated = _data(events, "skill_selection_gated")
    assert [(item["admitted"], item["basis"]) for item in gated] == [(True, "trial_judge")]
    assert judge.asked == [("请处理这个子任务", "leaf")]
    assert len(_data(events, "skill_dispatched")) == 1


async def test_trial_judge_rejection_blocks_dispatch(tmp_path: Path) -> None:
    """试用门拒绝：派发被拦，模型拿到拒绝理由。"""
    judge = _FixedJudge(approved=False)
    client, events = await _run(
        tmp_path,
        [_search(), _call("leaf", "tc_leaf"), _say("换个办法")],
        scores={"leaf": 0.6},
        gate=_gate(judge),
    )

    output = client.ledger.function_call_output_text("tc_leaf") or ""
    assert "selection_trial_rejected" in output
    assert "stub verdict" in output
    gated = _data(events, "skill_selection_gated")
    assert [(item["admitted"], item["basis"]) for item in gated] == [(False, "trial_rejected")]
    assert _data(events, "skill_dispatched") == []


async def test_read_skill_short_circuits_trial_judge(tmp_path: Path) -> None:
    """模型已读过说明书时不再问试用门。"""
    judge = _FixedJudge(approved=False)
    _client, events = await _run(
        tmp_path,
        [
            _search(),
            _read("leaf", "tc_read"),
            _call("leaf", "tc_leaf"),
            _say("leaf 完成"),
            _say("总结"),
        ],
        scores={"leaf": 0.6},
        gate=_gate(judge),
    )

    assert judge.asked == []
    gated = _data(events, "skill_selection_gated")
    assert [(item["admitted"], item["basis"]) for item in gated] == [(True, "read_skill")]


async def test_close_candidates_are_both_trial(tmp_path: Path) -> None:
    """前两名置信只差 0.02：都高于 tau_high 也一律先试用。"""
    client, events = await _run(
        tmp_path,
        [_search(), _call("leaf", "tc_blocked"), _say("放弃")],
        scores={"leaf": 0.9, "other": 0.88},
        gate=_gate(),
    )

    found = json.loads(client.ledger.function_call_output_text("tc_search") or "")
    assert {entry["skill_id"]: entry["route"] for entry in found} == {
        "leaf": "trial", "other": "trial",
    }
    assert "selection_needs_trial" in (
        client.ledger.function_call_output_text("tc_blocked") or ""
    )
    assert _data(events, "skill_dispatched") == []


# ====================================================================
# escalate：低置信不可派发
# ====================================================================


async def test_all_low_confidence_returns_no_match_and_blocks(tmp_path: Path) -> None:
    """全部候选低于 tau_low：召回返回 no_match，强行派发被拦。"""
    client, events = await _run(
        tmp_path,
        [_search(), _call("leaf", "tc_blocked"), _say("没有合适的 skill")],
        scores={"leaf": 0.2, "other": 0.1},
        gate=_gate(),
    )

    found = json.loads(client.ledger.function_call_output_text("tc_search") or "")
    assert found["no_match"] is True
    assert [entry["skill_id"] for entry in found["low_confidence"]] == ["leaf", "other"]
    assert all(entry["route"] == "escalate" for entry in found["low_confidence"])
    assert "selection_low_confidence" in (
        client.ledger.function_call_output_text("tc_blocked") or ""
    )
    gated = _data(events, "skill_selection_gated")
    assert [(item["admitted"], item["basis"]) for item in gated] == [(False, "low_confidence")]
    assert _data(events, "skill_dispatched") == []


async def test_reading_does_not_unlock_low_confidence(tmp_path: Path) -> None:
    """escalate 档读了说明书也不放行：要换关键词重搜。"""
    client, events = await _run(
        tmp_path,
        [
            _search(),
            _read("leaf", "tc_read"),
            _call("leaf", "tc_blocked"),
            _say("没有合适的 skill"),
        ],
        scores={"leaf": 0.2},
        gate=_gate(),
    )

    assert "selection_low_confidence" in (
        client.ledger.function_call_output_text("tc_blocked") or ""
    )
    assert _data(events, "skill_dispatched") == []


async def test_later_search_supersedes_earlier_route(tmp_path: Path) -> None:
    """同一 turn 里重搜后以最近一次召回的分流为准。"""

    class _Improving(_ScoredRecall):
        """第一次低置信，第二次高置信。"""

        def __init__(self) -> None:
            super().__init__({"leaf": 0.2})
            self._calls = 0

        async def recall(
            self,
            query: str,
            pool: Sequence[RecallEntry],
            *,
            top_k: int,
            cancel: CancellationToken,
        ) -> list[SkillCandidate]:
            """第二次起把 leaf 的置信抬到 0.9。"""
            self._calls += 1
            if self._calls > 1:
                self._scores = {"leaf": 0.9}
            return await super().recall(query, pool, top_k=top_k, cancel=cancel)

    client = SimClient(turns=[
        _search("tc_first"),
        _search("tc_second"),
        _call("leaf", "tc_leaf"),
        _say("leaf 完成"),
        _say("总结"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=_build_skills(tmp_path),
        threads_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        skill_recall=_Improving(),
        selection_gate=_gate(),
    )
    try:
        engine = await pool.get_or_create(session_id="s-gate", entry_skill_id="entry")
        events = await _drain(engine, taifeng.UserMessage(text="请处理这个子任务"))
    finally:
        await pool.close()

    gated = _data(events, "skill_selection_gated")
    assert [(item["admitted"], item["basis"]) for item in gated] == [(True, "route_proceed")]
    assert len(_data(events, "skill_dispatched")) == 1


# ====================================================================
# 分离派发走同一道门
# ====================================================================


async def test_spawn_skill_is_gated_like_call_skill(tmp_path: Path) -> None:
    """低置信候选改用 spawn_skill 也派不出去。"""
    client, events = await _run(
        tmp_path,
        [_search(), _spawn("leaf", "tc_spawn"), _say("没有合适的 skill")],
        scores={"leaf": 0.2},
        gate=_gate(),
    )

    assert "selection_low_confidence" in (
        client.ledger.function_call_output_text("tc_spawn") or ""
    )
    gated = _data(events, "skill_selection_gated")
    assert [(item["call_id"], item["admitted"]) for item in gated] == [("tc_spawn", False)]
    assert _data(events, "spawn_started") == []


async def test_spawn_skill_admits_high_confidence(tmp_path: Path) -> None:
    """高置信候选经 spawn_skill 正常分离派发。"""
    client, events = await _run(
        tmp_path,
        [_search(), _spawn("leaf", "tc_spawn"), _say("已发起"), _say("子任务完成")],
        scores={"leaf": 0.9},
        gate=_gate(),
    )

    output = json.loads(client.ledger.function_call_output_text("tc_spawn") or "")
    assert "handle_id" in output
    gated = _data(events, "skill_selection_gated")
    assert [(item["admitted"], item["basis"]) for item in gated] == [(True, "route_proceed")]


# ====================================================================
# 不受影响的路径
# ====================================================================


async def test_direct_whitelist_call_bypasses_gate(tmp_path: Path) -> None:
    """没有经过召回、直接派发白名单里的 skill：不过分流门。"""
    _client, events = await _run(
        tmp_path,
        [_call("leaf", "tc_leaf"), _say("leaf 完成"), _say("总结")],
        scores={"leaf": 0.1},
        gate=_gate(),
    )

    assert _data(events, "skill_selection_gated") == []
    assert len(_data(events, "skill_dispatched")) == 1


async def test_skill_absent_from_recall_bypasses_gate(tmp_path: Path) -> None:
    """召回结果里没有的 skill 不因别的候选低置信而被拦。"""
    _client, events = await _run(
        tmp_path,
        [_search(), _call("other", "tc_other"), _say("other 完成"), _say("总结")],
        scores={"leaf": 0.2},
        gate=_gate(),
    )

    assert _data(events, "skill_selection_gated") == []
    assert len(_data(events, "skill_dispatched")) == 1


async def test_recall_in_previous_turn_does_not_gate(tmp_path: Path) -> None:
    """上一轮的召回不约束下一轮的派发。"""
    client = SimClient(turns=[
        _search(),
        _say("本轮只搜索"),
        _call("leaf", "tc_leaf"),
        _say("leaf 完成"),
        _say("总结"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=_build_skills(tmp_path),
        threads_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        skill_recall=_ScoredRecall({"leaf": 0.2}),
        selection_gate=_gate(),
    )
    try:
        engine = await pool.get_or_create(session_id="s-gate", entry_skill_id="entry")
        await _drain(engine, taifeng.UserMessage(text="先搜一下"))
        events = await _drain(engine, taifeng.UserMessage(text="现在派发"))
    finally:
        await pool.close()

    assert _data(events, "skill_selection_gated") == []
    assert len(_data(events, "skill_dispatched")) == 1


async def test_pool_without_gate_keeps_recall_output(tmp_path: Path) -> None:
    """未启用分流门：召回结果不带分流字段，低置信候选照常可派发。"""
    client, events = await _run(
        tmp_path,
        [_search(), _call("leaf", "tc_leaf"), _say("leaf 完成"), _say("总结")],
        scores={"leaf": 0.2},
        gate=None,
    )

    found = json.loads(client.ledger.function_call_output_text("tc_search") or "")
    assert [sorted(entry) for entry in found] == [
        ["confidence", "description", "matched_snippet", "skill_id"]
    ]
    assert _data(events, "skill_selection_routed") == []
    assert _data(events, "skill_selection_gated") == []
    assert len(_data(events, "skill_dispatched")) == 1
