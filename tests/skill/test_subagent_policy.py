"""DispatchPolicy.subagent_approval_mode + _SubagentAutoDecisionPolicy 测试（G3 T1+T2）。

覆盖 spec ``skill-dispatch`` ADDED Requirements:
    - "DispatchPolicy 暴露 subagent_approval_mode 字段"
    - "_SubagentAutoDecisionPolicy 复用 inner rules 但跳过 prompter"
"""

from __future__ import annotations

import pytest

from taifeng.permission.types import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)
from taifeng.skill.dispatch import DispatchPolicy, _SubagentAutoDecisionPolicy


# --------------------------------------------------------------------
# DispatchPolicy 字段
# --------------------------------------------------------------------


def test_dispatch_policy_default_subagent_mode() -> None:
    pol = DispatchPolicy()
    assert pol.subagent_approval_mode == "inherit"


def test_dispatch_policy_rejects_invalid_subagent_mode() -> None:
    with pytest.raises(ValueError, match="subagent_approval_mode"):
        DispatchPolicy(subagent_approval_mode="strict")  # type: ignore[arg-type]


def test_dispatch_policy_accepts_three_valid_modes() -> None:
    for mode in ("inherit", "auto_deny", "auto_allow"):
        pol = DispatchPolicy(subagent_approval_mode=mode)  # type: ignore[arg-type]
        assert pol.subagent_approval_mode == mode


# --------------------------------------------------------------------
# _SubagentAutoDecisionPolicy 行为
# --------------------------------------------------------------------


def _req(scope: str = "tool_use", target: str = "x") -> PermissionRequest:
    return PermissionRequest(
        scope=scope, target=target, thread_id="t", submission_id="s",
        entry_skill_id="e", turn_index=1,
    )


class _NeverCalledPrompter:
    """断言永不被调用的 prompter；任何 prompt 调用都失败。"""

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        raise AssertionError(
            "wrapper should never call prompter (got: %r)" % (request,)
        )


@pytest.mark.asyncio
async def test_auto_deny_rule_allow_passes_through() -> None:
    inner = PermissionPolicy(
        rules=[PermissionRule(
            scope="tool_use", target_pattern="x", mode="allow",
        )],
        default_mode="ask",
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="deny")
    d = await wrapper.check(_req())
    assert d.granted is True
    assert d.mode == "allow"


@pytest.mark.asyncio
async def test_auto_deny_rule_deny_passes_through() -> None:
    inner = PermissionPolicy(
        rules=[PermissionRule(
            scope="tool_use", target_pattern="x", mode="deny",
        )],
        default_mode="ask",
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="deny")
    d = await wrapper.check(_req())
    assert d.granted is False
    assert d.mode == "deny"


@pytest.mark.asyncio
async def test_auto_deny_rule_ask_becomes_deny() -> None:
    inner = PermissionPolicy(
        rules=[PermissionRule(
            scope="tool_use", target_pattern="x", mode="ask",
        )],
        default_mode="allow",  # 故意混入：rule 优先
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="deny")
    d = await wrapper.check(_req())
    assert d.granted is False
    assert d.reason == "subagent_auto_deny"


@pytest.mark.asyncio
async def test_auto_deny_no_rule_default_ask_becomes_deny() -> None:
    inner = PermissionPolicy(
        rules=[],
        default_mode="ask",
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="deny")
    d = await wrapper.check(_req())
    assert d.granted is False
    assert d.reason == "subagent_auto_deny"


@pytest.mark.asyncio
async def test_auto_allow_rule_ask_becomes_allow() -> None:
    inner = PermissionPolicy(
        rules=[PermissionRule(
            scope="tool_use", target_pattern="x", mode="ask",
        )],
        default_mode="deny",
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="allow")
    d = await wrapper.check(_req())
    assert d.granted is True
    assert d.reason == "subagent_auto_allow"


@pytest.mark.asyncio
async def test_auto_allow_no_rule_default_ask_becomes_allow() -> None:
    inner = PermissionPolicy(
        rules=[], default_mode="ask",
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="allow")
    d = await wrapper.check(_req())
    assert d.granted is True
    assert d.reason == "subagent_auto_allow"


@pytest.mark.asyncio
async def test_inner_default_allow_is_respected() -> None:
    """无 rule + default_mode=allow → inner 决策优先，不走 fallback。"""
    inner = PermissionPolicy(
        rules=[],
        default_mode="allow",
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="deny")
    d = await wrapper.check(_req())
    assert d.granted is True
    assert d.reason == "inner_default_allow"


@pytest.mark.asyncio
async def test_inner_default_deny_is_respected() -> None:
    inner = PermissionPolicy(
        rules=[],
        default_mode="deny",
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="allow")
    d = await wrapper.check(_req())
    assert d.granted is False
    assert d.reason == "inner_default_deny"


@pytest.mark.asyncio
async def test_wrapper_does_not_call_prompter_even_with_match() -> None:
    """ask rule 命中时不调 prompter（关键场景）。"""
    inner = PermissionPolicy(
        rules=[PermissionRule(
            scope="shell_exec", target_pattern="re:^ls ", mode="ask",
        )],
        default_mode="deny",
        prompter=_NeverCalledPrompter(),
    )
    wrapper = _SubagentAutoDecisionPolicy(inner=inner, fallback="deny")
    d = await wrapper.check(_req(scope="shell_exec", target="ls /etc"))
    # _NeverCalledPrompter 抛 AssertionError 会被 pytest 报错；这里到达即 pass
    assert d.granted is False
    assert d.reason == "subagent_auto_deny"
