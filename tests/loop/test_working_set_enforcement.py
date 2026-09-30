"""按战绩规划的工作集生效端到端测试（认知回路相位 5，skill-working-set，ADR 0090）。

经 ``DispatchPolicy(working_set=..., trust=...)`` 启用后：

- 每条战绩落定后工作集重算，变更打成事件；
- 召回模式下工作集里的 child 直接列在 system prompt 里；
- 被隔离的 skill 从 child 列表与召回池里消失，配置为 ``block`` 时拒绝派发；
- 结论在 turn 开始时取定，turn 中途不改变 system prompt；
- 战绩记录与召回结果带来源信任层级。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit_config import (
    AuditCapabilityError,
    AuditStaticInputs,
    _validate_unsupported_fields,
)
from taifeng.skill import DispatchPolicy
from taifeng.skill.fitness import InMemorySkillFitnessStore
from taifeng.skill.outcome import SkillExecutionRecord
from taifeng.skill.recall import SkillCandidate
from taifeng.skill.trust import SourceTrustPolicy
from taifeng.skill.working_set import WorkingSetPolicy
from taifeng.skill.working_set_runtime import SkillWorkingSet
from tests.conftest import GUARD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.recall import RecallEntry

_USAGE = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
_PROVEN = "proven track record"

_ENTRY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [alpha, beta, bad]
tool_names: []
max_call_depth: 6
exposure:
  child_recall: {recall}
---
# ENTRY_BODY_MARK
"""

_CHILD = """---
name: {name}
description: 处理 {name} 类子任务
version: 1.0.0
type: atomic
---
# {mark}
"""


def _skills(tmp_path: Path, recall: str = "deferred") -> Path:
    """entry → alpha / beta / bad；bad 没有对应的模拟剧本，派发必失败。"""
    root = tmp_path / "skills"
    (root / "entry").mkdir(parents=True)
    (root / "entry" / "SKILL.md").write_text(_ENTRY.format(recall=recall), encoding="utf-8")
    for name in ("alpha", "beta", "bad"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(
            _CHILD.format(name=name, mark=f"{name.upper()}_BODY_MARK"), encoding="utf-8"
        )
    return root


class _Recall:
    """返回池内全部候选，并记录每次召回看到的池。"""

    def __init__(self) -> None:
        self.pools: list[list[str]] = []

    async def recall(
        self,
        query: str,
        pool: Sequence[RecallEntry],
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        self.pools.append([entry.skill_id for entry in pool])
        return [
            SkillCandidate(
                skill_id=entry.skill_id, description=entry.description, score=1.0,
                confidence=0.9, matched_snippet=None,
            )
            for entry in pool[:top_k]
        ]


def _record(skill_id: str, call_id: str, outcome: str) -> SkillExecutionRecord:
    return SkillExecutionRecord(
        skill_id=skill_id, call_id=call_id, parent_call_id=None, depth=1, source="user",
        trust_tier=None, selection_origin="whitelist", selection_confidence=None,
        outcome=outcome, outcome_signal_source="structural",  # type: ignore[arg-type]
        end_reason="completed", error_detail=None, cost_tokens=10, cost_duration_ms=5,
        cost_iterations=1, ts_unix=100)


def _policy(**kwargs: Any) -> WorkingSetPolicy:
    defaults: dict[str, Any] = {
        "budget": 2, "promote_min_score": 0.2, "promote_min_samples": 1,
        "quarantine_min_samples": 2, "quarantine_max_success_rate": 0.0,
    }
    return WorkingSetPolicy(**{**defaults, **kwargs})


def _call(skill_id: str, call_id: str) -> SimTurn:
    arguments = json.dumps({"reason": "需要它", "skill_id": skill_id, "args": {}})
    return SimTurn(
        text=f"派发 {skill_id}",
        tool_calls=[{"id": call_id, "name": "call_skill", "arguments": arguments}],
        usage=_USAGE,
    )


def _search(call_id: str) -> SimTurn:
    return SimTurn(
        text="搜索",
        tool_calls=[{
            "id": call_id, "name": "search_skills", "arguments": '{"query": "子任务"}',
        }],
        usage=_USAGE,
    )


def _say(text: str) -> SimTurn:
    return SimTurn(text=text, usage=_USAGE)


class _Run:
    """一次测试用到的 pool / engine / client。"""

    def __init__(self) -> None:
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.client: RoutingSimClient
        self.recall = _Recall()

    async def start(
        self,
        tmp_path: Path,
        *,
        entry: list[SimTurn],
        children: dict[str, list[SimTurn]] | None = None,
        dispatch_policy: DispatchPolicy | None = None,
        recall: str = "deferred",
    ) -> None:
        routes = {"ENTRY_BODY_MARK": entry}
        for name, turns in (children or {}).items():
            routes[f"{name.upper()}_BODY_MARK"] = turns
        self.client = RoutingSimClient(routes=routes)
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path, recall), threads_dir=tmp_path / "threads",
            model_client=self.client, compressors=[], skill_recall=self.recall,
            dispatch_policy=dispatch_policy,
        )
        self.engine = await self.pool.get_or_create(session_id="s", entry_skill_id="entry")

    async def ask(self, text: str = "请处理") -> list[taifeng.EventMsg]:
        """提交一条用户消息并收集事件，直到最外层 turn 终结。"""
        holder: list[str] = []
        events: list[taifeng.EventMsg] = []

        async def collect() -> None:
            depth = 0
            async for event in self.engine.subscribe_all():
                if not holder or event.submission_id != holder[0]:
                    continue
                events.append(event)
                if event.msg.kind == "skill_dispatched":
                    depth += 1
                elif event.msg.kind == "skill_returned":
                    depth -= 1
                if event.msg.kind in ("turn_completed", "turn_failed") and depth <= 0:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        holder.append(await self.engine.submit(taifeng.UserMessage(text=text)))
        await asyncio.wait_for(task, timeout=GUARD_TIMEOUT_SECONDS)
        return events

    def entry_prompts(self) -> list[str]:
        """entry 每次采样时的 system prompt（保序）。"""
        return [
            text
            for text in (
                "\n".join(request.system_texts()) for request in self.client.ledger.requests()
            )
            if "ENTRY_BODY_MARK" in text
        ]

    def output(self, call_id: str) -> str:
        return self.client.ledger.function_call_output_text(call_id) or ""


def _data(events: list[taifeng.EventMsg], kind: str) -> list[dict[str, Any]]:
    return [dict(event.msg.data) for event in events if event.msg.kind == kind]


def _working_set(store: InMemorySkillFitnessStore | None = None, **kwargs: Any) -> SkillWorkingSet:
    effect = kwargs.pop("quarantine_effect", "hide")
    return SkillWorkingSet(
        store=store or InMemorySkillFitnessStore(), policy=_policy(**kwargs),
        quarantine_effect=effect,
    )


# ====================================================================
# 提拔
# ====================================================================


async def test_promoted_child_is_listed_from_the_next_turn(tmp_path: Path) -> None:
    working_set = _working_set()
    run = _Run()
    await run.start(
        tmp_path,
        entry=[_call("alpha", "tc_alpha"), _say("第一轮完成"), _say("第二轮完成")],
        children={"alpha": [_say("alpha 完成")]},
        dispatch_policy=DispatchPolicy(working_set=working_set, trust=SourceTrustPolicy()),
    )

    first = await run.ask("第一轮")

    outcome = _data(first, "skill_outcome_recorded")[0]
    assert outcome["trust_tier"] == "standard"
    promoted = _data(first, "skill_promoted")
    # 触发重算的执行 = 那条战绩记录的 call_id
    assert [(p["skill_id"], p["trigger_call_id"], p["trust_tier"]) for p in promoted] == [
        ("alpha", outcome["call_id"], "standard"),
    ]
    assert promoted[0]["decided_samples"] == 1
    # 结论在 turn 开始时取定：本轮的两次采样都还看不到
    assert len(run.entry_prompts()) == 2
    assert all(_PROVEN not in prompt for prompt in run.entry_prompts())

    await run.ask("第二轮")

    prompt = run.entry_prompts()[-1]
    assert _PROVEN in prompt
    assert "- `alpha`: 处理 alpha 类子任务" in prompt
    assert "- `beta`" not in prompt
    # 仍是召回模式：其余 child 靠搜索
    assert "search_skills" in prompt
    await run.pool.close()


async def test_inline_list_is_not_reordered_by_promotion(tmp_path: Path) -> None:
    """inline 模式本来就全列：提拔不改变列表。"""
    store = InMemorySkillFitnessStore()
    await store.record(_record("beta", "b1", "success"))
    run = _Run()
    await run.start(
        tmp_path, entry=[_say("完成")], recall="inline",
        dispatch_policy=DispatchPolicy(working_set=_working_set(store)),
    )

    events = await run.ask()

    prompt = run.entry_prompts()[-1]
    assert _PROVEN not in prompt
    assert prompt.index("- `alpha`") < prompt.index("- `bad`") < prompt.index("- `beta`")
    # 启动时由已有战绩重算出的变更同样打事件，没有触发它的执行
    assert [(p["skill_id"], p["trigger_call_id"]) for p in _data(events, "skill_promoted")] == [
        ("beta", None),
    ]
    await run.pool.close()


# ====================================================================
# 隔离
# ====================================================================


async def _failing_store() -> InMemorySkillFitnessStore:
    store = InMemorySkillFitnessStore()
    await store.record(_record("bad", "b1", "failure"))
    await store.record(_record("bad", "b2", "failure"))
    return store


async def test_quarantined_skill_is_hidden_from_recall(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path,
        entry=[_search("tc_search"), _call("bad", "tc_bad"), _say("完成")],
        dispatch_policy=DispatchPolicy(working_set=_working_set(await _failing_store())),
    )

    events = await run.ask()

    assert [q["skill_id"] for q in _data(events, "skill_quarantined")] == ["bad"]
    assert run.recall.pools == [["alpha", "beta"]]
    found = json.loads(run.output("tc_search"))
    assert [entry["skill_id"] for entry in found] == ["alpha", "beta"]
    # 提示里的可选 child 数不含被隔离的
    assert "共 2 个" in run.entry_prompts()[0]
    # hide 只是对模型隐藏：按 id 直接派发仍会执行（这里 bad 照旧失败）
    assert _data(events, "skill_dispatched")[0]["skill_id"] == "bad"
    assert run.output("tc_bad").startswith("sub_skill_failed")
    await run.pool.close()


async def test_quarantined_skill_is_hidden_from_inline_list(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path, entry=[_say("完成")], recall="inline",
        dispatch_policy=DispatchPolicy(working_set=_working_set(await _failing_store())),
    )

    await run.ask()

    prompt = run.entry_prompts()[-1]
    assert "- `alpha`" in prompt
    assert "- `bad`" not in prompt
    await run.pool.close()


async def test_flagged_skill_stays_visible(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path, entry=[_search("tc_search"), _say("完成")],
        dispatch_policy=DispatchPolicy(
            working_set=_working_set(await _failing_store(), quarantine_effect="flag")
        ),
    )

    events = await run.ask()

    assert [q["skill_id"] for q in _data(events, "skill_quarantined")] == ["bad"]
    assert run.recall.pools == [["alpha", "bad", "beta"]]
    await run.pool.close()


async def test_blocked_skill_is_not_dispatched(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path, entry=[_call("bad", "tc_bad"), _say("换个办法")],
        dispatch_policy=DispatchPolicy(
            working_set=_working_set(await _failing_store(), quarantine_effect="block")
        ),
    )

    events = await run.ask()

    assert run.output("tc_bad") == (
        "dispatch_rejected: skill_quarantined (path: entry → bad)"
    )
    assert _data(events, "skill_dispatched") == []
    assert _data(events, "skill_outcome_recorded") == []
    await run.pool.close()


async def test_failures_in_flow_quarantine_the_skill(tmp_path: Path) -> None:
    store = InMemorySkillFitnessStore()
    await store.record(_record("bad", "b1", "failure"))
    run = _Run()
    await run.start(
        tmp_path,
        entry=[_call("bad", "tc_bad"), _say("第一轮完成"), _search("tc_search"), _say("完成")],
        dispatch_policy=DispatchPolicy(working_set=_working_set(store)),
    )

    first = await run.ask("第一轮")

    outcome = _data(first, "skill_outcome_recorded")[0]
    quarantined = _data(first, "skill_quarantined")
    assert [(q["skill_id"], q["trigger_call_id"]) for q in quarantined] == [
        ("bad", outcome["call_id"]),
    ]
    assert quarantined[0]["success_rate"] == 0.0
    assert quarantined[0]["decided_samples"] == 2

    await run.ask("第二轮")

    assert run.recall.pools == [["alpha", "beta"]]
    await run.pool.close()


# ====================================================================
# 未启用 / 只配信任策略
# ====================================================================


async def test_without_working_set_nothing_changes(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path,
        entry=[_call("alpha", "tc_alpha"), _say("第一轮完成"), _search("tc_search"), _say("完成")],
        children={"alpha": [_say("alpha 完成")]},
    )

    first = await run.ask("第一轮")
    await run.ask("第二轮")

    assert _data(first, "skill_promoted") == []
    assert _data(first, "skill_outcome_recorded")[0]["trust_tier"] is None
    assert all(_PROVEN not in prompt for prompt in run.entry_prompts())
    assert run.recall.pools == [["alpha", "bad", "beta"]]
    found = json.loads(run.output("tc_search"))
    assert all("trust_tier" not in entry for entry in found)
    await run.pool.close()


async def test_trust_policy_alone_labels_records_and_candidates(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path,
        entry=[_search("tc_search"), _call("alpha", "tc_alpha"), _say("完成")],
        children={"alpha": [_say("alpha 完成")]},
        dispatch_policy=DispatchPolicy(
            trust=SourceTrustPolicy(overrides={"beta": "untrusted"})
        ),
    )

    events = await run.ask()

    found = json.loads(run.output("tc_search"))
    assert {entry["skill_id"]: entry["trust_tier"] for entry in found} == {
        "alpha": "standard", "bad": "standard", "beta": "untrusted",
    }
    assert _data(events, "skill_outcome_recorded")[0]["trust_tier"] == "standard"
    assert _data(events, "skill_promoted") == []
    await run.pool.close()


# ====================================================================
# 审计模式
# ====================================================================


def test_audit_mode_rejects_working_set() -> None:
    """工作集的结论不在审计 Journal 里，却会改变 prompt：静态门拒绝。"""
    inputs = AuditStaticInputs(
        model_client=object(),  # type: ignore[arg-type]
        skill_snapshot=object(),  # type: ignore[arg-type]
        failure_suspension_enabled=False,
        skill_suspension_enabled=False,
        skill_working_set=_working_set(),
    )

    with pytest.raises(AuditCapabilityError) as raised:
        _validate_unsupported_fields(inputs)

    assert raised.value.code == "audit_skill_working_set_unsupported"
