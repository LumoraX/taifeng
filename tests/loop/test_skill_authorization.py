"""白名单外 skill 授权端到端测试（认知回路相位 4，skill-authorization，ADR 0089）。

经 ``DispatchPolicy(authorization=...)`` 启用后：

- ``search_skills`` 的召回池包含白名单外可发现的 skill，结果里标 ``requires_authorization``；
- 即便 child 列表是 inline，也暴露 ``search_skills`` 并在 system prompt 里说明；
- ``call_skill`` 派发白名单外的 skill 前过授权，放行后其余的门照常执行；
- 未启用时白名单是硬边界，行为不变。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit_config import AuditCapabilityError
from taifeng.loop.submission import Resume
from taifeng.permission.types import (
    PermissionPolicy,
    PermissionRule,
    SuspendingPrompter,
)
from taifeng.skill import DispatchPolicy
from taifeng.skill.authorization import (
    CallbackSkillAuthorization,
    PermissionSkillAuthorization,
    SkillAuthorizationDecision,
    SkillAuthorizationRequest,
)
from taifeng.skill.recall import SkillCandidate
from taifeng.skill.selection import SkillSelectionGate, ThresholdSelectionPolicy
from taifeng.tool.builtins.spawn_skill import make_spawn_skill_tool
from tests.conftest import GUARD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.recall import RecallEntry

_USAGE = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
_TERMINAL = ("turn_completed", "turn_failed", "turn_suspended", "suspension_resolve_rejected")

_ENTRY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [inside]
tool_names: [spawn_skill]
max_call_depth: 6
exposure:
  child_recall: inline
---
# ENTRY_MARK
"""

_CHILD = """---
name: {name}
description: {name} 处理具体子任务
version: 1.0.0
type: atomic
---
# {name} 的说明书
"""


def _skills(tmp_path: Path) -> Path:
    """entry（白名单只有 inside）+ 白名单外的 outsider / sealed。"""
    root = tmp_path / "skills"
    (root / "entry").mkdir(parents=True)
    (root / "entry" / "SKILL.md").write_text(_ENTRY, encoding="utf-8")
    for name in ("inside", "outsider", "sealed"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(_CHILD.format(name=name), encoding="utf-8")
    return root


class _ScoredRecall:
    """对池内每个候选给固定置信度，并记录每次召回看到的池。"""

    def __init__(self, confidence: float = 0.9) -> None:
        self._confidence = confidence
        self.pools: list[list[str]] = []

    async def recall(
        self,
        query: str,
        pool: Sequence[RecallEntry],
        *,
        top_k: int,
        cancel: CancellationToken,
    ) -> list[SkillCandidate]:
        """返回池内全部候选。"""
        self.pools.append([entry.skill_id for entry in pool])
        return [
            SkillCandidate(
                skill_id=entry.skill_id, description=entry.description, score=1.0,
                confidence=self._confidence, matched_snippet=None,
            )
            for entry in pool[:top_k]
        ]


class _Authorizer:
    """记录授权请求的授权策略：sealed 不可发现，其余按构造参数裁决。"""

    def __init__(self, granted: bool) -> None:
        self.requests: list[SkillAuthorizationRequest] = []
        self.policy = CallbackSkillAuthorization(
            self._decide,
            discoverable=lambda _caller, candidate: candidate.id != "sealed",
        )
        self._granted = granted

    async def _decide(self, request: SkillAuthorizationRequest) -> SkillAuthorizationDecision:
        self.requests.append(request)
        if self._granted:
            return SkillAuthorizationDecision.allow("entitled")
        return SkillAuthorizationDecision.deny("no entitlement")


def _tool(name: str, call_id: str, **arguments: Any) -> SimTurn:
    """模型调一次工具。"""
    return SimTurn(
        text=f"调用 {name}",
        tool_calls=[{
            "id": call_id, "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }],
        usage=_USAGE,
    )


def _search(call_id: str = "tc_search") -> SimTurn:
    return _tool("search_skills", call_id, query="子任务")


def _call(skill_id: str, call_id: str) -> SimTurn:
    return _tool("call_skill", call_id, reason="需要它的能力", skill_id=skill_id, args={})


def _say(text: str) -> SimTurn:
    return SimTurn(text=text, usage=_USAGE)


class _Run:
    """一次测试用到的 pool / engine / client。"""

    def __init__(self) -> None:
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.client: SimClient
        self.recall = _ScoredRecall()

    async def start(self, tmp_path: Path, turns: list[SimTurn], **kwargs: Any) -> None:
        self.client = SimClient(turns=turns)
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), threads_dir=tmp_path / "threads",
            model_client=self.client, compressors=[], skill_recall=self.recall,
            extra_tools=[make_spawn_skill_tool()], **kwargs,
        )
        self.engine = await self.pool.get_or_create(session_id="s", entry_skill_id="entry")

    async def submit(self, op: Any) -> list[taifeng.EventMsg]:
        """提交并收集事件，直到最外层 turn 终结或挂起。"""
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
                if event.msg.kind in _TERMINAL and depth <= 0:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        holder.append(await self.engine.submit(op))
        await asyncio.wait_for(task, timeout=GUARD_TIMEOUT_SECONDS)
        return events

    async def ask(self, text: str = "请处理这个子任务") -> list[taifeng.EventMsg]:
        return await self.submit(taifeng.UserMessage(text=text))

    def output(self, call_id: str) -> str:
        return self.client.ledger.function_call_output_text(call_id) or ""


def _data(events: list[taifeng.EventMsg], kind: str) -> list[dict[str, Any]]:
    return [dict(event.msg.data) for event in events if event.msg.kind == kind]


def _kinds(events: list[taifeng.EventMsg]) -> list[str]:
    return [event.msg.kind for event in events]


def _policy(authorizer: _Authorizer) -> DispatchPolicy:
    return DispatchPolicy(authorization=authorizer.policy)


# ====================================================================
# 未启用：白名单是硬边界
# ====================================================================


async def test_without_authorization_whitelist_is_a_hard_boundary(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [_call("outsider", "tc_out"), _say("换个办法")])

    events = await run.ask()

    assert run.output("tc_out").startswith("dispatch_rejected: not_in_whitelist")
    assert _data(events, "skill_dispatched") == []
    request = run.client.ledger.last_request()
    assert request is not None
    # inline 的小白名单不暴露 search_skills，prompt 里也没有白名单外发现的说明
    assert "search_skills" not in request.tool_names()
    assert "requires_authorization" not in "\n".join(request.system_texts())
    await run.pool.close()


# ====================================================================
# 发现
# ====================================================================


async def test_search_reaches_outside_the_whitelist(tmp_path: Path) -> None:
    authorizer = _Authorizer(granted=True)
    run = _Run()
    await run.start(
        tmp_path, [_search(), _say("看完了")], dispatch_policy=_policy(authorizer)
    )

    events = await run.ask()

    found = json.loads(run.output("tc_search"))
    assert [entry["skill_id"] for entry in found] == ["inside", "outsider"]
    assert "requires_authorization" not in found[0]
    assert found[1]["requires_authorization"] is True
    # sealed 不在可发现范围内，entry 自己与别的入口也不在
    assert run.recall.pools == [["inside", "outsider"]]
    invoked = _data(events, "skill_search_invoked")[0]
    assert (invoked["pool_size"], invoked["outside_pool_size"]) == (2, 1)
    request = run.client.ledger.last_request()
    assert request is not None
    assert "search_skills" in request.tool_names()
    system = "\n".join(request.system_texts())
    assert "requires_authorization" in system
    # inline 的 child 列表照常列出
    assert "- `inside`" in system
    # 发现不等于授权：没有派发就没有授权请求
    assert authorizer.requests == []
    await run.pool.close()


async def test_discovered_outsider_can_be_read(tmp_path: Path) -> None:
    authorizer = _Authorizer(granted=True)
    run = _Run()
    await run.start(
        tmp_path,
        [
            _tool("read_skill", "tc_read", skill_id="outsider"),
            _tool("read_skill", "tc_sealed", skill_id="sealed"),
            _say("读完了"),
        ],
        dispatch_policy=_policy(authorizer),
    )

    await run.ask()

    assert "outsider 的说明书" in run.output("tc_read")
    assert run.output("tc_sealed").startswith("skill_not_visible")
    assert authorizer.requests == []
    await run.pool.close()


# ====================================================================
# 准入
# ====================================================================


async def test_authorized_outsider_is_dispatched(tmp_path: Path) -> None:
    authorizer = _Authorizer(granted=True)
    run = _Run()
    await run.start(
        tmp_path,
        [_search(), _call("outsider", "tc_out"), _say("outsider 完成"), _say("总结")],
        dispatch_policy=_policy(authorizer),
        request_metadata={"x_corr": "v-9"},
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", _kinds(events)
    assert "outsider 完成" in run.output("tc_out")
    request = authorizer.requests[0]
    assert (request.caller_skill_id, request.target_skill_id) == ("entry", "outsider")
    assert request.origin == "call_skill"
    assert request.reason == "需要它的能力"
    assert request.call_chain == ("entry",)
    assert request.call_id == "tc_out"
    assert request.thread_id == run.engine.thread_id
    assert request.entry_skill_id == "entry"
    assert request.metadata == {"x_corr": "v-9"}
    assert request.target_description == "outsider 处理具体子任务"
    granted = _data(events, "skill_authorization_granted")
    assert granted == [{
        "caller_skill_id": "entry", "target_skill_id": "outsider", "call_id": "tc_out",
        "origin": "call_skill", "reason": "entitled", "request_reason": "需要它的能力",
        "call_chain": ["entry"],
    }]
    assert [d["skill_id"] for d in _data(events, "skill_dispatched")] == ["outsider"]
    # 经发现选中：战绩里记为 discovered
    outcome = _data(events, "skill_outcome_recorded")[0]
    assert (outcome["skill_id"], outcome["selection_origin"]) == ("outsider", "discovered")
    await run.pool.close()


async def test_denied_outsider_is_not_dispatched(tmp_path: Path) -> None:
    authorizer = _Authorizer(granted=False)
    run = _Run()
    await run.start(
        tmp_path,
        [_search(), _call("outsider", "tc_out"), _say("换个办法")],
        dispatch_policy=_policy(authorizer),
    )

    events = await run.ask()

    assert run.output("tc_out") == "skill_authorization_denied: no entitlement"
    denied = _data(events, "skill_authorization_denied")
    assert [(d["target_skill_id"], d["reason"]) for d in denied] == [
        ("outsider", "no entitlement")
    ]
    assert _data(events, "skill_authorization_granted") == []
    assert _data(events, "skill_dispatched") == []
    assert _data(events, "skill_outcome_recorded") == []
    await run.pool.close()


async def test_undiscoverable_target_keeps_the_whitelist_rejection(tmp_path: Path) -> None:
    """不在可发现范围内的目标：拒绝与未启用时逐字相同，不触发授权。"""
    authorizer = _Authorizer(granted=True)
    run = _Run()
    await run.start(
        tmp_path,
        [
            _call("sealed", "tc_sealed"),
            _call("ghost", "tc_ghost"),
            _say("换个办法"),
        ],
        dispatch_policy=_policy(authorizer),
    )

    events = await run.ask()

    assert run.output("tc_sealed") == (
        "dispatch_rejected: not_in_whitelist (path: entry → sealed)"
    )
    assert run.output("tc_ghost").startswith("dispatch_rejected: unknown_skill")
    assert authorizer.requests == []
    assert _data(events, "skill_authorization_denied") == []
    await run.pool.close()


async def test_whitelisted_child_needs_no_authorization(tmp_path: Path) -> None:
    authorizer = _Authorizer(granted=False)
    run = _Run()
    await run.start(
        tmp_path,
        [_call("inside", "tc_in"), _say("inside 完成"), _say("总结")],
        dispatch_policy=_policy(authorizer),
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    assert authorizer.requests == []
    assert [d["skill_id"] for d in _data(events, "skill_dispatched")] == ["inside"]
    await run.pool.close()


async def test_authorization_error_fails_closed(tmp_path: Path) -> None:
    """授权后端出错：按工具故障处理，不派发。"""

    async def broken(request: SkillAuthorizationRequest) -> SkillAuthorizationDecision:
        raise RuntimeError("entitlement service down")

    run = _Run()
    await run.start(
        tmp_path,
        [_call("outsider", "tc_out"), _say("换个办法")],
        dispatch_policy=DispatchPolicy(authorization=CallbackSkillAuthorization(broken)),
    )

    events = await run.ask()

    assert "entitlement service down" in run.output("tc_out")
    assert _data(events, "skill_dispatched") == []
    assert _data(events, "skill_authorization_granted") == []
    await run.pool.close()


async def test_detached_spawn_stays_within_the_whitelist(tmp_path: Path) -> None:
    """分离派发不走白名单外授权。"""
    authorizer = _Authorizer(granted=True)
    run = _Run()
    await run.start(
        tmp_path,
        [
            _tool("spawn_skill", "tc_spawn", reason="并发", skill_id="outsider", args={}),
            _say("换个办法"),
        ],
        dispatch_policy=_policy(authorizer),
    )

    await run.ask()

    assert run.output("tc_spawn").startswith("spawn_rejected: not_in_whitelist")
    assert authorizer.requests == []
    await run.pool.close()


# ====================================================================
# 与其余的门叠加
# ====================================================================


async def test_authorization_does_not_bypass_permission_gate(tmp_path: Path) -> None:
    """授权放行后，skill_dispatch 的拒绝规则仍然生效。"""
    authorizer = _Authorizer(granted=True)
    run = _Run()
    await run.start(
        tmp_path,
        [_call("outsider", "tc_out"), _say("换个办法")],
        dispatch_policy=_policy(authorizer),
        permission_policy=PermissionPolicy(
            rules=[PermissionRule(
                scope="skill_dispatch", target_pattern="outsider", mode="deny",
                reason="frozen",
            )],
            default_mode="allow",
        ),
    )

    events = await run.ask()

    assert len(authorizer.requests) == 1
    assert run.output("tc_out") == "skill_dispatch_denied: frozen"
    assert _data(events, "skill_dispatched") == []
    await run.pool.close()


async def test_selection_gate_applies_to_outsiders(tmp_path: Path) -> None:
    """白名单外的中置信候选：先试用，再授权。"""
    authorizer = _Authorizer(granted=True)
    run = _Run()
    run.recall = _ScoredRecall(confidence=0.6)
    await run.start(
        tmp_path,
        [
            _search(),
            _call("outsider", "tc_blocked"),
            _tool("read_skill", "tc_read", skill_id="outsider"),
            _call("outsider", "tc_out"),
            _say("outsider 完成"),
            _say("总结"),
        ],
        dispatch_policy=_policy(authorizer),
        selection_gate=SkillSelectionGate(policy=ThresholdSelectionPolicy()),
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", _kinds(events)
    assert "selection_needs_trial" in run.output("tc_blocked")
    gated = _data(events, "skill_selection_gated")
    assert [(g["call_id"], g["admitted"]) for g in gated] == [
        ("tc_blocked", False), ("tc_out", True),
    ]
    assert [d["skill_id"] for d in _data(events, "skill_dispatched")] == ["outsider"]
    await run.pool.close()


# ====================================================================
# 用权限策略授权：规则 / 人工审批
# ====================================================================


async def test_permission_rule_authorizes(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path,
        [_call("outsider", "tc_out"), _say("outsider 完成"), _say("总结")],
        dispatch_policy=DispatchPolicy(authorization=PermissionSkillAuthorization()),
        permission_policy=PermissionPolicy.from_dict({
            "default_mode": "deny",
            "allow": ["SkillAuthorization(outsider)", "Skill(*)"],
        }),
    )

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed", _kinds(events)
    assert [d["skill_id"] for d in _data(events, "skill_dispatched")] == ["outsider"]
    await run.pool.close()


async def test_permission_authorization_without_policy_denies(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path,
        [_call("outsider", "tc_out"), _say("换个办法")],
        dispatch_policy=DispatchPolicy(authorization=PermissionSkillAuthorization()),
    )

    events = await run.ask()

    assert run.output("tc_out") == "skill_authorization_denied: no_permission_policy"
    assert _data(events, "skill_dispatched") == []
    await run.pool.close()


async def _suspended_request(run: _Run, events: list[taifeng.EventMsg]) -> Any:
    """断言 turn 挂起并返回唯一的待答请求。"""
    assert events[-1].msg.kind == "turn_suspended", _kinds(events)
    record = run.engine._find_active_suspension()  # noqa: SLF001
    assert record is not None
    assert len(record.pending) == 1
    return record.pending[0]


async def test_human_approval_by_suspension(tmp_path: Path) -> None:
    """授权走人工审批：挂起 → 批准 → 派发。skill_dispatch 已由规则放行，只问一次。"""
    run = _Run()
    await run.start(
        tmp_path,
        [_call("outsider", "tc_out"), _say("outsider 完成"), _say("总结")],
        dispatch_policy=DispatchPolicy(authorization=PermissionSkillAuthorization()),
        permission_policy=PermissionPolicy(
            rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
            default_mode="ask",
            prompter=SuspendingPrompter(),
        ),
    )

    pending = await _suspended_request(run, await run.ask())

    assert pending.related_call_id == "tc_out"
    assert pending.detail["scope"] == "skill_authorization"
    assert pending.detail["target"] == "outsider"
    assert pending.detail["caller_skill_id"] == "entry"

    resumed = await run.submit(Resume(
        thread_id=run.engine.thread_id, resolutions={pending.request_id: {"granted": True}},
    ))

    assert resumed[-1].msg.kind == "turn_completed", _kinds(resumed)
    assert [d["skill_id"] for d in _data(resumed, "skill_dispatched")] == ["outsider"]
    assert "outsider 完成" in run.output("tc_out")
    await run.pool.close()


async def test_human_denial_by_suspension(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path,
        [_call("outsider", "tc_out"), _say("换个办法")],
        dispatch_policy=DispatchPolicy(authorization=PermissionSkillAuthorization()),
        permission_policy=PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter()),
    )

    pending = await _suspended_request(run, await run.ask())
    resumed = await run.submit(Resume(
        thread_id=run.engine.thread_id,
        resolutions={pending.request_id: {"granted": False, "reason": "不合适"}},
    ))

    assert resumed[-1].msg.kind == "turn_completed", _kinds(resumed)
    assert _data(resumed, "skill_dispatched") == []
    await run.pool.close()


async def test_two_approvals_do_not_livelock(tmp_path: Path) -> None:
    """授权与 skill_dispatch 都要人批：问两次，各批一次，然后派发。"""
    run = _Run()
    await run.start(
        tmp_path,
        [_call("outsider", "tc_out"), _say("outsider 完成"), _say("总结")],
        dispatch_policy=DispatchPolicy(authorization=PermissionSkillAuthorization()),
        permission_policy=PermissionPolicy(default_mode="ask", prompter=SuspendingPrompter()),
    )

    first = await _suspended_request(run, await run.ask())
    assert first.detail["scope"] == "skill_authorization"

    second = await _suspended_request(run, await run.submit(Resume(
        thread_id=run.engine.thread_id, resolutions={first.request_id: {"granted": True}},
    )))
    assert second.detail["scope"] == "skill_dispatch"
    assert second.related_call_id == "tc_out"

    resumed = await run.submit(Resume(
        thread_id=run.engine.thread_id, resolutions={second.request_id: {"granted": True}},
    ))

    assert resumed[-1].msg.kind == "turn_completed", _kinds(resumed)
    assert [d["skill_id"] for d in _data(resumed, "skill_dispatched")] == ["outsider"]
    await run.pool.close()


# ====================================================================
# 审计模式
# ====================================================================


def test_audit_mode_rejects_authorization() -> None:
    """白名单外授权尚未接入审计 Journal：静态门拒绝。"""
    from taifeng.loop.audit_config import AuditStaticInputs, _validate_unsupported_fields

    inputs = AuditStaticInputs(
        model_client=object(),  # type: ignore[arg-type]
        skill_snapshot=object(),  # type: ignore[arg-type]
        failure_suspension_enabled=False,
        skill_suspension_enabled=False,
        skill_authorization=PermissionSkillAuthorization(),
    )

    with pytest.raises(AuditCapabilityError) as raised:
        _validate_unsupported_fields(inputs)

    assert raised.value.code == "audit_skill_authorization_unsupported"
