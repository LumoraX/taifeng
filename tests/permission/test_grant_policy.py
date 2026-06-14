"""permission-grants: PermissionPolicy.check 与 grant 的集成。

关键不变量：
    - grant 命中只绕过 prompter（"预先答好的 ask"），prompter 不被调用
    - grant **绝不**越过 deny 规则（deny 在 grant 之前裁决）
    - prompter 返回的 decision.grant 被记账，后续同模请求自动放行
    - _preapproved_call_ids 优先于 grant（resume 语义不被 grant 抢占）
    - grant 用尽（max_uses）后回落到 prompter
    - 签发 / 命中 / 失效三事件经 telemetry 回调上报
"""

from __future__ import annotations

from taifeng.permission import (
    PermissionDecision,
    PermissionGrant,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)


class _CountingPrompter:
    """记录被调用次数的 prompter；可配置返回 allow / deny。"""

    def __init__(self, *, decision: PermissionDecision) -> None:
        self.calls = 0
        self._decision = decision

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        self.calls += 1
        return self._decision


def _req(target: str = "x", *, call_id: str | None = None) -> PermissionRequest:
    """构造 custom scope 的审批请求（可带 call_id）。"""
    meta = {"call_id": call_id} if call_id is not None else {}
    return PermissionRequest(scope="custom", target=target, metadata=meta)


# ==============================================================
# 1. grant 命中绕过 prompter
# ==============================================================


async def test_grant_hit_bypasses_prompter() -> None:
    prompter = _CountingPrompter(decision=PermissionDecision.deny(reason="would_deny"))
    policy = PermissionPolicy(default_mode="ask", prompter=prompter)
    policy.issue_grant(PermissionGrant(scope="custom", target_pattern="x"))

    d = await policy.check(_req("x"))

    assert d.granted is True
    assert d.reason.startswith("grant:")
    assert prompter.calls == 0  # 绕过 prompter


# ==============================================================
# 2. grant 绝不越过 deny 规则
# ==============================================================


async def test_grant_never_overrides_deny_rule() -> None:
    prompter = _CountingPrompter(decision=PermissionDecision.allow())
    policy = PermissionPolicy(
        rules=[PermissionRule(scope="custom", target_pattern="x", mode="deny")],
        default_mode="ask",
        prompter=prompter,
    )
    policy.issue_grant(PermissionGrant(scope="custom", target_pattern="x"))

    d = await policy.check(_req("x"))

    assert d.granted is False  # deny 规则绝对，grant 顶不翻
    assert prompter.calls == 0  # deny 直接短路，连 prompter 都不到


# ==============================================================
# 3. decision.grant 被记账，后续自动放行
# ==============================================================


async def test_decision_grant_is_recorded_and_reused() -> None:
    # prompter 第一次被问时签发一张 grant
    grant = PermissionGrant(scope="custom", target_pattern="x", grant_id="g-reuse")
    prompter = _CountingPrompter(
        decision=PermissionDecision.allow(reason="human_yes", grant=grant)
    )
    policy = PermissionPolicy(default_mode="ask", prompter=prompter)

    d1 = await policy.check(_req("x"))
    assert d1.granted is True
    assert prompter.calls == 1  # 第一次问了人

    d2 = await policy.check(_req("x"))
    assert d2.granted is True
    assert d2.reason == "grant:g-reuse"
    assert prompter.calls == 1  # 第二次走 grant，没再问人


# ==============================================================
# 4. _preapproved_call_ids 优先于 grant
# ==============================================================


async def test_preapprove_precedes_grant() -> None:
    prompter = _CountingPrompter(decision=PermissionDecision.deny(reason="no"))
    policy = PermissionPolicy(default_mode="ask", prompter=prompter)
    # 同时存在：一张 max_uses=1 的 grant + 对该 call_id 的一次性预批
    policy.issue_grant(
        PermissionGrant(scope="custom", target_pattern="x", max_uses=1, grant_id="g1")
    )
    policy.preapprove("call-42")

    d = await policy.check(_req("x", call_id="call-42"))
    assert d.granted is True
    assert d.reason == "resume_preapproved"  # 走预批，而非 grant

    # grant 未被消耗：下一次（无预批）仍能命中它
    d2 = await policy.check(_req("x"))
    assert d2.reason == "grant:g1"


# ==============================================================
# 5. grant 用尽后回落 prompter
# ==============================================================


async def test_grant_falls_back_to_prompter_when_exhausted() -> None:
    prompter = _CountingPrompter(decision=PermissionDecision.deny(reason="no"))
    policy = PermissionPolicy(default_mode="ask", prompter=prompter)
    policy.issue_grant(
        PermissionGrant(scope="custom", target_pattern="x", max_uses=1, grant_id="g1")
    )

    d1 = await policy.check(_req("x"))
    assert d1.granted is True and prompter.calls == 0  # 第一次走 grant

    d2 = await policy.check(_req("x"))
    assert d2.granted is False and prompter.calls == 1  # grant 用尽，回落 prompter


# ==============================================================
# 6. telemetry 三事件
# ==============================================================


async def test_grant_telemetry_events() -> None:
    events: list[tuple[str, dict]] = []

    async def telemetry(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    grant = PermissionGrant(
        scope="custom", target_pattern="x", max_uses=1, grant_id="g-tel"
    )
    prompter = _CountingPrompter(
        decision=PermissionDecision.allow(reason="yes", grant=grant)
    )
    policy = PermissionPolicy(
        default_mode="ask", prompter=prompter, telemetry=telemetry
    )

    await policy.check(_req("x"))  # 触发 issued
    await policy.check(_req("x"))  # 触发 hit + expired（max_uses=1）

    kinds = [k for k, _ in events]
    assert "permission_grant_issued" in kinds
    assert "permission_grant_hit" in kinds
    assert "permission_grant_expired" in kinds
    # 事件载荷带 grant_id（审计可追溯）
    issued = next(p for k, p in events if k == "permission_grant_issued")
    assert issued["grant_id"] == "g-tel"


# ==============================================================
# 7. revoke_grant
# ==============================================================


async def test_revoke_grant() -> None:
    prompter = _CountingPrompter(decision=PermissionDecision.deny(reason="no"))
    policy = PermissionPolicy(default_mode="ask", prompter=prompter)
    policy.issue_grant(
        PermissionGrant(scope="custom", target_pattern="x", grant_id="g1")
    )

    assert policy.revoke_grant("g1") is True
    d = await policy.check(_req("x"))
    assert d.granted is False  # 已撤销 → 回落 prompter（deny）
