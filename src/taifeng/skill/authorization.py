"""白名单外 skill 的派发授权 —— 认知回路相位 4：准入（⑥）（skill-authorization，ADR 0089）。

``child_skills`` 白名单是作者预授权的工作集：列在里面的 skill 派发不需要再问。相位 4 让
调用方**够得到白名单之外**的 skill，前提是每一次派发都过授权：

```
发现   search_skills 的召回池 = 白名单内可见的 + 白名单外可发现的
准入   call_skill 目标不在白名单 → SkillAuthorizationPolicy.authorize
         ├─ 放行 → 继续过其余的门（深度 / 环 / 分流门 / hook / 权限审批）
         └─ 拒绝 → skill_authorization_denied
```

授权在准入，不在发现：可发现只决定模型能不能搜到、读到说明书；能不能派发由 ``authorize``
逐次裁决。授权不放宽其余任何一道门。

不注入 ``SkillAuthorizationPolicy`` 时白名单是硬边界，行为与此前完全一致。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from taifeng.loop.cancellation import CancellationToken
    from taifeng.permission.models import PermissionRequest
    from taifeng.permission.types import PermissionPolicy
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.eligibility import RuntimeCapabilities
    from taifeng.skill.registry import SkillSnapshot

AuthorizationOrigin = Literal["call_skill"]
"""发起授权请求的派发入口。"""

REQUIRES_AUTHORIZATION_FIELD = "requires_authorization"
"""``search_skills`` 结果里标记「该候选在白名单之外、派发须经授权」的键。"""

_RESUME_PREAPPROVED = "resume_preapproved"
_REMEMBERED_APPROVALS = 256


@dataclass(frozen=True)
class SkillAuthorizationRequest:
    """一次白名单外派发的授权请求。

    Attributes:
        caller_skill_id: 发起派发的 skill。
        target_skill_id: 派发目标（不在 caller 的白名单内）。
        target_description: 目标的描述。
        target_source: 目标的来源（``SkillDefinition.source``）。
        origin: 派发入口。
        reason: 模型自陈的派发理由。
        call_chain: 当前调用栈，最深的在最后。
        call_id: 本次工具调用的 id。
        thread_id / submission_id / entry_skill_id / turn_index: 所属会话上下文。
        metadata: 业务透传的上下文（``request_metadata``）；内核不解析其中的键。
        permission_policy: 当前生效的权限策略（子 turn 内是按 subagent 模式包装后的那个）；
            未配置时为 None。
    """

    caller_skill_id: str
    target_skill_id: str
    target_description: str
    target_source: str
    origin: AuthorizationOrigin
    reason: str
    call_chain: tuple[str, ...]
    call_id: str
    thread_id: str = ""
    submission_id: str = ""
    entry_skill_id: str = ""
    turn_index: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    permission_policy: PermissionPolicy | None = None


@dataclass(frozen=True)
class SkillAuthorizationDecision:
    """授权裁决。"""

    granted: bool
    reason: str = ""

    @classmethod
    def allow(cls, reason: str = "") -> SkillAuthorizationDecision:
        """放行。"""
        return cls(granted=True, reason=reason)

    @classmethod
    def deny(cls, reason: str) -> SkillAuthorizationDecision:
        """拒绝；理由会回给模型并进入事件。"""
        return cls(granted=False, reason=reason)


@runtime_checkable
class SkillAuthorizationPolicy(Protocol):
    """白名单外 skill 的授权协议：内核给参考实现，业务可注入自己的口径。"""

    def discoverable(self, caller: SkillDefinition, candidate: SkillDefinition) -> bool:
        """``candidate`` 能否被 ``caller`` 在白名单之外发现。

        SHALL 是无副作用的同步判断：每轮组装工具清单、每次召回都会调用。
        """
        ...

    async def authorize(
        self, request: SkillAuthorizationRequest, *, cancel: CancellationToken,
    ) -> SkillAuthorizationDecision:
        """裁决一次白名单外派发。实现 MUST 可取消（R4）。"""
        ...


class CallbackSkillAuthorization:
    """把授权交给业务回调（如「该调用方有没有这个 skill 的权限」的接口）。"""

    def __init__(
        self,
        authorize: Callable[[SkillAuthorizationRequest], Awaitable[SkillAuthorizationDecision]],
        *,
        discoverable: Callable[[SkillDefinition, SkillDefinition], bool] | None = None,
    ) -> None:
        """
        Args:
            authorize: 授权回调。
            discoverable: 可发现范围；None = 注册表里的 skill 都可被发现。
        """
        self._authorize = authorize
        self._discoverable = discoverable

    def discoverable(self, caller: SkillDefinition, candidate: SkillDefinition) -> bool:
        """按注入的范围判断；未注入时恒可发现。"""
        return True if self._discoverable is None else self._discoverable(caller, candidate)

    async def authorize(
        self, request: SkillAuthorizationRequest, *, cancel: CancellationToken,
    ) -> SkillAuthorizationDecision:
        """调用业务回调；调用前检查取消。"""
        cancel.raise_if_cancelled()
        return await self._authorize(request)


class PermissionSkillAuthorization:
    """用当前生效的 ``PermissionPolicy`` 授权：规则、可复用授权与人工审批都沿用既有机制。

    授权请求的权限范围是 ``skill_authorization``（与白名单内派发的 ``skill_dispatch`` 分开），
    目标是被派发的 skill id。未配置权限策略时一律拒绝。

    人工审批以挂起的方式进行时，批准后内核会重跑这次调用；本类记住已获批准的调用，
    使重跑时后续的门（如 ``skill_dispatch`` 审批）能各自拿到自己的批准。
    """

    def __init__(
        self,
        *,
        discoverable: Callable[[SkillDefinition, SkillDefinition], bool] | None = None,
    ) -> None:
        """
        Args:
            discoverable: 可发现范围；None = 注册表里的 skill 都可被发现。
        """
        self._discoverable = discoverable
        self._approved: OrderedDict[tuple[str, str], None] = OrderedDict()

    def discoverable(self, caller: SkillDefinition, candidate: SkillDefinition) -> bool:
        """按注入的范围判断；未注入时恒可发现。"""
        return True if self._discoverable is None else self._discoverable(caller, candidate)

    async def authorize(
        self, request: SkillAuthorizationRequest, *, cancel: CancellationToken,
    ) -> SkillAuthorizationDecision:
        """向权限策略发一次 ``skill_authorization`` 请求。"""
        cancel.raise_if_cancelled()
        key = (request.thread_id, request.call_id)
        if key in self._approved:
            # 这次调用的授权已由人批准过：重跑时不再占用新的批准
            del self._approved[key]
            return SkillAuthorizationDecision.allow("approved_before_resume")
        if request.permission_policy is None:
            return SkillAuthorizationDecision.deny("no_permission_policy")
        decision = await request.permission_policy.check(_permission_request(request))
        if decision.granted and decision.reason == _RESUME_PREAPPROVED:
            self._remember(key)
        if decision.granted:
            return SkillAuthorizationDecision.allow(decision.reason)
        return SkillAuthorizationDecision.deny(decision.reason or "permission_denied")

    def _remember(self, key: tuple[str, str]) -> None:
        """记住一次经人批准的授权；超出上限时丢弃最早的。"""
        self._approved[key] = None
        while len(self._approved) > _REMEMBERED_APPROVALS:
            self._approved.popitem(last=False)


def _permission_request(request: SkillAuthorizationRequest) -> PermissionRequest:
    """把授权请求转成权限请求。"""
    # 延迟 import：permission 包的初始化不应成为 skill 包的 import 期依赖
    from taifeng.permission.models import PermissionRequest

    return PermissionRequest(
        scope="skill_authorization",
        target=request.target_skill_id,
        reason=request.reason,
        metadata={
            **request.metadata,
            "caller_skill_id": request.caller_skill_id,
            "target_source": request.target_source,
            "origin": request.origin,
            "call_id": request.call_id,
        },
        thread_id=request.thread_id,
        submission_id=request.submission_id,
        entry_skill_id=request.entry_skill_id,
        call_chain=request.call_chain,
        turn_index=request.turn_index,
    )


@dataclass(frozen=True)
class DiscoverableSkill:
    """一个白名单外可发现的 skill（id + description）。"""

    skill_id: str
    description: str


def _excluded(caller: SkillDefinition, on_stack: Sequence[str]) -> set[str]:
    """不参与白名单外发现的 skill：白名单内的、``caller`` 自己、调用栈上的。"""
    return {caller.id, *caller.child_skills, *on_stack}


def _admissible(
    caller: SkillDefinition,
    candidate: SkillDefinition | None,
    policy: SkillAuthorizationPolicy,
    capabilities: RuntimeCapabilities | None,
) -> bool:
    """单个候选是否可在白名单外被发现：先过内核的可见性过滤，再问授权策略。"""
    # 延迟 import 避免与 eligibility 形成 import 期循环依赖
    from taifeng.skill.eligibility import is_skill_eligible

    if candidate is None or candidate.entry:
        return False
    if not candidate.exposure.model_invocable:
        return False
    if capabilities is not None and not is_skill_eligible(candidate, capabilities):
        return False
    return policy.discoverable(caller, candidate)


def discoverable_outside(
    caller: SkillDefinition,
    snapshot: SkillSnapshot,
    policy: SkillAuthorizationPolicy | None,
    capabilities: RuntimeCapabilities | None = None,
    *,
    on_stack: Sequence[str] = (),
) -> list[DiscoverableSkill]:
    """``caller`` 在白名单之外能发现的 skill，按 id 升序。

    内核先施加与白名单内相同的可见性过滤，再问授权策略：

    - 已在白名单内、``caller`` 自己、调用栈上的 skill（派发必然成环）不算；
    - entry skill 不算（``call_skill`` 不能把入口作为子调用）；
    - ``exposure.model_invocable == False``、``requires`` 不满足的不算；
    - 其余由 ``policy.discoverable`` 决定。

    Args:
        policy: 授权策略；None = 未启用相位 4，恒返回空列表。
        on_stack: 当前调用栈上的 skill id。
    """
    if policy is None:
        return []
    found: list[DiscoverableSkill] = []
    for skill_id in sorted(snapshot.ids() - _excluded(caller, on_stack)):
        candidate = snapshot.get(skill_id)
        if candidate is not None and _admissible(caller, candidate, policy, capabilities):
            found.append(DiscoverableSkill(candidate.id, candidate.description))
    return found


def is_discoverable_outside(
    caller: SkillDefinition,
    target_id: str,
    snapshot: SkillSnapshot,
    policy: SkillAuthorizationPolicy | None,
    capabilities: RuntimeCapabilities | None = None,
    *,
    on_stack: Sequence[str] = (),
) -> bool:
    """``target_id`` 是否在 ``caller`` 的白名单外可发现范围内。"""
    if policy is None or target_id in _excluded(caller, on_stack):
        return False
    return _admissible(caller, snapshot.get(target_id), policy, capabilities)


__all__ = [
    "REQUIRES_AUTHORIZATION_FIELD",
    "AuthorizationOrigin",
    "CallbackSkillAuthorization",
    "DiscoverableSkill",
    "PermissionSkillAuthorization",
    "SkillAuthorizationDecision",
    "SkillAuthorizationPolicy",
    "SkillAuthorizationRequest",
    "discoverable_outside",
    "is_discoverable_outside",
]
