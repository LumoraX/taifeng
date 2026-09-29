"""白名单外 skill 授权的纯逻辑测试（相位 4，skill-authorization，ADR 0089）。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionGrant,
    PermissionPolicy,
    PermissionRule,
)
from taifeng.skill import CallStack, DispatchPolicy, SkillDefinition
from taifeng.skill.authorization import (
    CallbackSkillAuthorization,
    PermissionSkillAuthorization,
    SkillAuthorizationDecision,
    SkillAuthorizationPolicy,
    SkillAuthorizationRequest,
    discoverable_outside,
    is_discoverable_outside,
)
from taifeng.skill.definition import SkillExposure
from taifeng.skill.registry import SkillSnapshot

if TYPE_CHECKING:
    from taifeng.permission import PermissionRequest


def _skill(
    skill_id: str,
    *,
    child: frozenset[str] = frozenset(),
    entry: bool = False,
    invocable: bool = True,
) -> SkillDefinition:
    """构造一个最小 skill 定义。"""
    return SkillDefinition(
        id=skill_id,
        name=skill_id,
        description=f"skill {skill_id}",
        version="1",
        body="",
        body_path=Path("/nonexistent") / skill_id / "SKILL.md",
        type="composite" if (child or entry) else "atomic",
        entry=entry,
        child_skills=child,
        exposure=SkillExposure(model_invocable=invocable),
    )


def _snapshot(*skills: SkillDefinition) -> SkillSnapshot:
    """全互通的 snapshot。"""
    ids = frozenset(skill.id for skill in skills)
    return SkillSnapshot(
        version=1, skills=tuple(skills), reachable_graph={skill.id: ids for skill in skills}
    )


def _request(policy: PermissionPolicy | None, call_id: str = "c1") -> SkillAuthorizationRequest:
    """一次 caller → outsider 的授权请求。"""
    return SkillAuthorizationRequest(
        caller_skill_id="caller",
        target_skill_id="outsider",
        target_description="skill outsider",
        target_source="user",
        origin="call_skill",
        reason="需要它的能力",
        call_chain=("caller",),
        call_id=call_id,
        thread_id="t-1",
        submission_id="s-1",
        entry_skill_id="caller",
        turn_index=2,
        metadata={"x_corr": "v-1"},
        permission_policy=policy,
    )


async def _allow_all(request: SkillAuthorizationRequest) -> SkillAuthorizationDecision:
    """恒放行的授权回调。"""
    return SkillAuthorizationDecision.allow("ok")


# ------------------------------------------------------------------
# 可发现范围
# ------------------------------------------------------------------


def test_no_policy_means_nothing_is_discoverable() -> None:
    caller = _skill("caller", child=frozenset({"inside"}), entry=True)
    snapshot = _snapshot(caller, _skill("inside"), _skill("outsider"))

    assert discoverable_outside(caller, snapshot, None) == []
    assert is_discoverable_outside(caller, "outsider", snapshot, None) is False


def test_discoverable_excludes_whitelist_self_entry_and_hidden() -> None:
    caller = _skill("caller", child=frozenset({"inside"}), entry=True)
    snapshot = _snapshot(
        caller,
        _skill("inside"),
        _skill("outsider"),
        _skill("another"),
        _skill("other-entry", entry=True),
        _skill("hidden", invocable=False),
    )
    policy = CallbackSkillAuthorization(_allow_all)

    found = discoverable_outside(caller, snapshot, policy)

    assert [item.skill_id for item in found] == ["another", "outsider"]
    assert found[0].description == "skill another"
    for skill_id in ("inside", "caller", "other-entry", "hidden", "ghost"):
        assert is_discoverable_outside(caller, skill_id, snapshot, policy) is False


def test_discoverable_excludes_skills_on_call_stack() -> None:
    caller = _skill("caller", entry=True)
    snapshot = _snapshot(caller, _skill("outsider"), _skill("ancestor"))
    policy = CallbackSkillAuthorization(_allow_all)

    found = discoverable_outside(caller, snapshot, policy, on_stack=("ancestor", "caller"))

    assert [item.skill_id for item in found] == ["outsider"]
    assert is_discoverable_outside(
        caller, "ancestor", snapshot, policy, on_stack=("ancestor", "caller")
    ) is False


def test_policy_narrows_discoverable_range() -> None:
    caller = _skill("caller", entry=True)
    snapshot = _snapshot(caller, _skill("report-a"), _skill("ops-b"))
    policy = CallbackSkillAuthorization(
        _allow_all,
        discoverable=lambda _caller, candidate: candidate.id.startswith("report-"),
    )

    assert [i.skill_id for i in discoverable_outside(caller, snapshot, policy)] == ["report-a"]
    assert is_discoverable_outside(caller, "ops-b", snapshot, policy) is False


def test_builtin_policies_satisfy_protocol() -> None:
    assert isinstance(CallbackSkillAuthorization(_allow_all), SkillAuthorizationPolicy)
    assert isinstance(PermissionSkillAuthorization(), SkillAuthorizationPolicy)


# ------------------------------------------------------------------
# DispatchPolicy：授权只豁免白名单一层
# ------------------------------------------------------------------


def test_authorized_flag_skips_only_the_whitelist_check() -> None:
    caller = _skill("caller", child=frozenset({"inside"}), entry=True)
    outsider = _skill("outsider")
    stack = CallStack().push("caller", "1")
    policy = DispatchPolicy()

    assert policy.check(stack, caller, outsider).reason == "not_in_whitelist"
    assert policy.check(stack, caller, outsider, authorized_outside_whitelist=True).allowed


def test_authorized_flag_keeps_structural_checks() -> None:
    caller = _skill("caller", entry=True)
    stack = CallStack().push("caller", "1")
    policy = DispatchPolicy()

    entry_target = policy.check(
        stack, caller, _skill("other-entry", entry=True), authorized_outside_whitelist=True
    )
    unknown = policy.check(stack, caller, None, authorized_outside_whitelist=True)
    cycle = policy.check(
        stack.push("outsider", "2"), caller, _skill("outsider"),
        authorized_outside_whitelist=True,
    )
    shallow = SkillDefinition(
        id="shallow", name="shallow", description="d", version="1", body="",
        body_path=Path("/nonexistent/shallow/SKILL.md"), type="composite", entry=True,
        max_call_depth=1,
    )
    depth = policy.check(
        CallStack().push("shallow", "1"), shallow, _skill("outsider"),
        authorized_outside_whitelist=True,
    )

    assert entry_target.reason == "cannot_call_entry_skill"
    assert unknown.reason == "unknown_skill"
    assert cycle.reason == "cycle_detected"
    assert depth.reason == "max_depth_exceeded"


# ------------------------------------------------------------------
# CallbackSkillAuthorization
# ------------------------------------------------------------------


async def test_callback_authorization_passes_request_through() -> None:
    seen: list[SkillAuthorizationRequest] = []

    async def decide(request: SkillAuthorizationRequest) -> SkillAuthorizationDecision:
        seen.append(request)
        return SkillAuthorizationDecision.deny("no entitlement")

    decision = await CallbackSkillAuthorization(decide).authorize(
        _request(None), cancel=CancellationToken()
    )

    assert decision == SkillAuthorizationDecision(granted=False, reason="no entitlement")
    assert seen[0].target_skill_id == "outsider"


async def test_callback_authorization_honours_cancellation() -> None:
    cancel = CancellationToken()
    cancel.cancel()

    with pytest.raises(BaseException) as raised:  # noqa: PT011
        await CallbackSkillAuthorization(_allow_all).authorize(_request(None), cancel=cancel)

    assert "cancel" in type(raised.value).__name__.lower()


# ------------------------------------------------------------------
# PermissionSkillAuthorization
# ------------------------------------------------------------------


async def test_permission_authorization_without_policy_denies() -> None:
    decision = await PermissionSkillAuthorization().authorize(
        _request(None), cancel=CancellationToken()
    )

    assert decision.granted is False
    assert decision.reason == "no_permission_policy"


async def test_permission_authorization_uses_its_own_scope() -> None:
    seen: list[PermissionRequest] = []

    async def prompt(request: PermissionRequest) -> PermissionDecision:
        seen.append(request)
        return PermissionDecision.allow(reason="human said yes")

    policy = PermissionPolicy(default_mode="ask", prompter=CallbackPrompter(prompt))

    decision = await PermissionSkillAuthorization().authorize(
        _request(policy), cancel=CancellationToken()
    )

    assert decision == SkillAuthorizationDecision(granted=True, reason="human said yes")
    request = seen[0]
    assert request.scope == "skill_authorization"
    assert request.target == "outsider"
    assert request.reason == "需要它的能力"
    assert request.call_chain == ("caller",)
    assert (request.thread_id, request.submission_id) == ("t-1", "s-1")
    assert (request.entry_skill_id, request.turn_index) == ("caller", 2)
    assert request.metadata == {
        "x_corr": "v-1", "caller_skill_id": "caller", "target_source": "user",
        "target_trust_tier": None, "origin": "call_skill", "call_id": "c1",
    }


async def test_skill_dispatch_rules_do_not_authorize() -> None:
    """白名单内派发的放行规则不等于白名单外授权。"""
    policy = PermissionPolicy(
        rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
        default_mode="deny",
    )

    decision = await PermissionSkillAuthorization().authorize(
        _request(policy), cancel=CancellationToken()
    )

    assert decision.granted is False


async def test_rule_alias_and_deny_rule() -> None:
    policy = PermissionPolicy.from_dict({
        "default_mode": "deny",
        "allow": ["SkillAuthorization(out*)"],
    })
    denied = PermissionPolicy.from_dict({
        "default_mode": "allow",
        "deny": ["SkillAuthorization(outsider)"],
    })
    authorization = PermissionSkillAuthorization()

    assert (await authorization.authorize(_request(policy), cancel=CancellationToken())).granted
    assert not (
        await authorization.authorize(_request(denied), cancel=CancellationToken())
    ).granted


async def test_reusable_grant_authorizes_without_prompt() -> None:
    policy = PermissionPolicy(default_mode="ask")
    policy.issue_grant(PermissionGrant(
        scope="skill_authorization", target_pattern="outsider", max_uses=1,
    ))
    authorization = PermissionSkillAuthorization()

    first = await authorization.authorize(_request(policy), cancel=CancellationToken())
    second = await authorization.authorize(_request(policy, "c2"), cancel=CancellationToken())

    assert first.granted is True
    assert first.reason.startswith("grant:")
    # 授权用尽、又没有 prompter：保守拒绝
    assert second.granted is False


async def test_resume_approval_is_not_spent_twice() -> None:
    """人批准后重跑：授权这道门只占用一次批准，后面的门仍能拿到自己的批准。"""
    policy = PermissionPolicy(default_mode="ask")
    authorization = PermissionSkillAuthorization()

    policy.preapprove("c1")
    first = await authorization.authorize(_request(policy), cancel=CancellationToken())
    # 后面的门挂起、人再次批准、整次调用重跑
    policy.preapprove("c1")
    second = await authorization.authorize(_request(policy), cancel=CancellationToken())

    assert first.granted and second.granted
    assert second.reason == "approved_before_resume"
    # 第二次批准没有被授权这道门消费
    assert "c1" in policy._preapproved_call_ids  # noqa: SLF001
    # 记住的批准只用一次
    third = await authorization.authorize(_request(policy, "c1"), cancel=CancellationToken())
    assert third.reason == "resume_preapproved"
