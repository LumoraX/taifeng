"""审计模式下的 hook 与权限裁决：先落账，再生效（ADR 0096）。

业务注入的 hook 与权限策略原样保留，内核在它们外面按 turn 绑定一层：

```text
handler(hook, ctx)  ──►  hook_evaluated 落账（ack）  ──►  调用方拿到裁决
policy.check(req)   ──►  permission_decided 落账（ack） ──►  调用方拿到裁决
```

绑定在 runner 构造时完成（``bind_audit_gates``），子 skill 的 runner 重新绑定到自己的 thread。
裁决记录的 operation identity 是 ``{turn_id}:hook:{n}`` / ``{turn_id}:permission:{n}``，
序号按 turn 在整个 Session 内连续分配：同一 turn 的 engine 层 hook（``pre_turn`` / ``post_turn``）
与 runner 层 hook 共用一个计数。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from taifeng.conversation.journal.canonical import validate_json_value
from taifeng.conversation.journal.errors import NonCanonicalValueError
from taifeng.conversation.journal.gate_records import (
    HOOK_EVALUATED_RECORD_TYPE,
    HOOK_OVERRIDE_KEYS,
    PERMISSION_DECIDED_RECORD_TYPE,
    HookEvaluatedV1,
    PermissionDecidedV1,
)
from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import JournalIdentities, JournalRecordFactory
from taifeng.hooks.types import HookDecision
from taifeng.permission.models import PermissionDecision
from taifeng.suspend.signal import SuspendSignal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.conversation.journal.records import PayloadModel
    from taifeng.hooks.types import HookContext, HookKind, HookRunner
    from taifeng.loop.audit import SessionAuditCoordinator
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.permission.models import PermissionRequest

logger = logging.getLogger(__name__)

_NOT_CANONICAL = "audit_permission_request_not_canonical"

# Session → {(thread, submission, turn, kind) → 下一个序号}
_ORDINALS: WeakKeyDictionary[SessionAuditCoordinator, dict[tuple[str, str, int, str], int]] = (
    WeakKeyDictionary()
)

# hook 输入里用来指认「这次裁决针对谁」的字段
_SUBJECT_FIELDS = (
    "call_id", "tool_name", "target_skill_id", "caller_skill_id", "script_name",
    "skill_id", "phase", "iteration", "end_reason", "depth",
)


class AuditTurnScope:
    """一个 turn 的审计落账入口：session 状态 + turn 标识。"""

    def __init__(self, state: AuditedSessionState, submission_id: str, turn_index: int) -> None:
        """
        Args:
            state: 所在 thread 的审计状态。
            submission_id / turn_index: 所属 turn。
        """
        self.state = state
        self.submission_id = submission_id
        self.turn_index = turn_index

    def same_turn(self, other: AuditTurnScope) -> bool:
        """两个入口是否指向同一个 thread 上的同一个 turn。"""
        return (
            self.state.thread_id == other.state.thread_id
            and self.submission_id == other.submission_id
            and self.turn_index == other.turn_index
        )

    def _next_ordinal(self, kind: str) -> int:
        """分配该 turn 内这一类记录的下一个序号（同步段，不会被并发打断）。"""
        counters = _ORDINALS.setdefault(self.state.coordinator, {})
        key = (self.state.thread_id, self.submission_id, self.turn_index, kind)
        ordinal = counters.get(key, 0)
        counters[key] = ordinal + 1
        return ordinal

    async def commit(self, kind: str, record_type: str, payload: PayloadModel) -> None:
        """落一条裁决记录；返回即已 durable。

        Raises:
            SessionAuditFrozenError: Journal 写入不确定，Session 已冻结。
        """
        coordinator = self.state.coordinator
        await coordinator.ensure_effect_allowed()
        identities = JournalIdentities(
            coordinator.session_id, self.state.thread_id, self.submission_id
        )
        turn_id = identities.turn(self.turn_index)
        factory = JournalRecordFactory(
            session_id=coordinator.session_id,
            actor=ActorRef(kind="system", source=kind),
            identities=identities,
        )
        record = factory.build(
            operation_id=identities.context(turn_id, kind, self._next_ordinal(kind)),
            record_type=record_type,
            payload=payload,
            submission_id=self.submission_id,
            thread_id=self.state.thread_id,
            turn_id=turn_id,
        )
        await coordinator.append(record)


def _canonical_or_none(value: object) -> Any:
    """值能进 Journal 就原样返回，否则返回 None。"""
    try:
        return validate_json_value(value)
    except NonCanonicalValueError:
        return None


def _subject(hook: object) -> dict[str, Any]:
    """从 hook 输入里取出指认对象的字段。"""
    fields = asdict(hook) if is_dataclass(hook) and not isinstance(hook, type) else {}
    return {
        name: fields[name]
        for name in _SUBJECT_FIELDS
        if name in fields and isinstance(fields[name], (str, int, bool))
    }


def _overrides(decision: HookDecision) -> dict[str, Any]:
    """handler 要求的改写里能进 Journal 的部分。"""
    found: dict[str, Any] = {}
    for key in HOOK_OVERRIDE_KEYS:
        if key not in decision.metadata:
            continue
        value = _canonical_or_none(decision.metadata[key])
        if value is None and decision.metadata[key] is not None:
            # 改写内容进不了 Journal：记下这一点，调用方照常拿到原始裁决
            found[key] = {"not_canonical": True}
        else:
            found[key] = value
    return found


class _AuditedRegistry:
    """把业务的 hook 注册表包一层：每个 handler 的裁决先落账再返回。"""

    def __init__(self, inner: Any, scope: AuditTurnScope) -> None:
        self._inner = inner
        self._scope = scope

    def handlers(self, kind: HookKind) -> list[Callable[[Any, HookContext], Awaitable[HookDecision]]]:
        """该类型下全部 handler，顺序与注册顺序一致。"""
        return [
            self._wrap(kind, index, handler)
            for index, handler in enumerate(self._inner.handlers(kind))
        ]

    def _wrap(
        self,
        kind: HookKind,
        index: int,
        handler: Callable[[Any, HookContext], Awaitable[HookDecision]],
    ) -> Callable[[Any, HookContext], Awaitable[HookDecision]]:
        scope = self._scope

        async def audited(hook: Any, ctx: HookContext) -> HookDecision:
            try:
                decision = await handler(hook, ctx)
            except SuspendSignal:
                raise
            except Exception as exc:
                # handler 出错：调用方各有处置（多数按拒绝处理），这里只把事实记下来
                await scope.commit("hook", HOOK_EVALUATED_RECORD_TYPE, HookEvaluatedV1(
                    hook_kind=kind, handler_index=index, allow=False,
                    reason="hook_error", subject=_subject(hook),
                    error_class=type(exc).__name__,
                ))
                raise
            await scope.commit("hook", HOOK_EVALUATED_RECORD_TYPE, HookEvaluatedV1(
                hook_kind=kind, handler_index=index, allow=decision.allow,
                reason=decision.reason, subject=_subject(hook),
                overrides=_overrides(decision),
                metadata_keys=tuple(sorted(
                    key for key in decision.metadata if key not in HOOK_OVERRIDE_KEYS
                )),
            ))
            return decision

        return audited

    def __getattr__(self, name: str) -> Any:
        """其余属性（注册、注销等）转给业务的注册表。"""
        return getattr(self._inner, name)


class AuditedHookRunner:
    """绑定到一个 turn 的 hook 运行器：接口与 ``HookRunner`` 相同。"""

    def __init__(self, inner: HookRunner, scope: AuditTurnScope) -> None:
        """
        Args:
            inner: 业务注入的 hook 运行器。
            scope: 裁决落账的去处。
        """
        self.inner = inner
        self.scope = scope
        self._registry = _AuditedRegistry(inner.registry, scope)

    @property
    def registry(self) -> _AuditedRegistry:
        """裁决会落账的注册表视图。"""
        return self._registry

    async def run(self, kind: HookKind, hook: Any, ctx: HookContext) -> HookDecision:
        """串行运行；任一 handler 拒绝或出错即整体拒绝（与 ``HookRunner.run`` 同语义）。"""
        for handler in self._registry.handlers(kind):
            try:
                decision = await handler(hook, ctx)
            except SuspendSignal:
                raise
            except Exception as exc:
                if _is_frozen(exc):
                    raise
                logger.exception("hook %s raised", kind)
                return HookDecision.deny(f"hook_error: {exc}")
            if not decision.allow:
                return decision
        return HookDecision.ok()

    async def run_audit_only(self, kind: HookKind, hook: Any, ctx: HookContext) -> None:
        """串行运行；拒绝与出错都不影响调用方（与 ``HookRunner.run_audit_only`` 同语义）。"""
        for handler in self._registry.handlers(kind):
            try:
                await handler(hook, ctx)
            except Exception as exc:
                if _is_frozen(exc):
                    raise
                logger.exception("audit-only hook %s raised (ignored)", kind)


def _is_frozen(exc: BaseException) -> bool:
    """是不是 Session 冻结：冻结不能被当成 hook 自己的错误吞掉。"""
    from taifeng.loop.audit_support import SessionAuditFrozenError

    return isinstance(exc, SessionAuditFrozenError)


class AuditedPermissionPolicy:
    """绑定到一个 turn 的权限策略：``check`` 的裁决先落账再返回，其余原样转给业务的策略。"""

    def __init__(self, inner: Any, scope: AuditTurnScope) -> None:
        """
        Args:
            inner: 业务注入的权限策略（或按 subagent 模式包装后的策略）。
            scope: 裁决落账的去处。
        """
        self.inner = inner
        self.scope = scope

    async def check(self, request: PermissionRequest) -> PermissionDecision:
        """裁决一次权限请求。

        请求携带的上下文进不了 Journal 时拒绝这次请求：放行一个无法记录的请求等于放弃审计。
        """
        metadata = _canonical_or_none(dict(request.metadata))
        if metadata is None:
            decision = PermissionDecision.deny(reason=_NOT_CANONICAL)
            metadata = {}
        else:
            decision = await self.inner.check(request)
        call_id = request.metadata.get("call_id")
        await self.scope.commit(
            "permission", PERMISSION_DECIDED_RECORD_TYPE, PermissionDecidedV1(
                scope=request.scope,
                target=request.target,
                request_reason=request.reason,
                call_id=call_id if isinstance(call_id, str) and call_id else None,
                call_chain=tuple(request.call_chain),
                request_metadata=metadata,
                granted=decision.granted,
                mode=decision.mode,
                decision_reason=decision.reason,
                remember_until=decision.remember_until,
                minted_grant=_grant_summary(decision),
            ),
        )
        return decision

    def __getattr__(self, name: str) -> Any:
        """``rules`` / ``default_mode`` / ``preapprove`` / ``issue_grant`` 等转给业务的策略。"""
        return getattr(self.inner, name)


def _grant_summary(decision: PermissionDecision) -> dict[str, Any] | None:
    """随裁决签发的可复用授权的匹配条件。"""
    grant = decision.grant
    if grant is None:
        return None
    return {
        "scope": grant.scope,
        "target_pattern": grant.target_pattern,
        "args_match": dict(grant.args_match) if grant.args_match is not None else None,
        "max_uses": grant.max_uses,
        "call_chain_prefix": list(grant.call_chain_prefix),
        "thread_id": grant.thread_id,
        "grant_id": grant.grant_id,
        "reason": grant.reason,
    }


def audited_hooks(hooks: Any, scope: AuditTurnScope) -> Any:
    """把 hook 运行器绑定到 ``scope``；已绑定到同一 turn 的原样返回，None 仍是 None。"""
    if hooks is None:
        return None
    if isinstance(hooks, AuditedHookRunner):
        return hooks if hooks.scope.same_turn(scope) else AuditedHookRunner(hooks.inner, scope)
    return AuditedHookRunner(hooks, scope)


def audited_permission(policy: Any, scope: AuditTurnScope) -> Any:
    """把权限策略绑定到 ``scope``；已绑定到同一 turn 的原样返回，None 仍是 None。"""
    if policy is None:
        return None
    if isinstance(policy, AuditedPermissionPolicy):
        if policy.scope.same_turn(scope):
            return policy
        return AuditedPermissionPolicy(policy.inner, scope)
    return AuditedPermissionPolicy(policy, scope)


def engine_turn_hooks(engine: Any, submission_id: str, turn_index: int | None) -> Any:
    """engine 层（runner 之外）触发 hook 时用的运行器：审计模式下绑定到该 turn。

    Args:
        engine: 宿主 engine。
        submission_id: 所属 submission。
        turn_index: 该 turn 的序号；None = 用 engine 当前的序号。
    """
    state = engine._audit_state  # noqa: SLF001
    if state is None:
        return engine._hooks  # noqa: SLF001
    index = engine._turn_index if turn_index is None else turn_index  # noqa: SLF001
    return audited_hooks(engine._hooks, AuditTurnScope(state, submission_id, index))  # noqa: SLF001


def bind_audit_gates(runner: Any) -> None:
    """审计模式下把 runner 的 hook 与权限策略绑定到它自己的 turn；非审计模式什么都不做。"""
    state = getattr(runner, "audit_state", None)
    if state is None:
        return
    scope = AuditTurnScope(state, runner.submission_id, runner.turn_index)
    runner.hooks = audited_hooks(runner.hooks, scope)
    runner.permission_policy = audited_permission(runner.permission_policy, scope)


__all__ = [
    "AuditTurnScope",
    "AuditedHookRunner",
    "AuditedPermissionPolicy",
    "audited_hooks",
    "audited_permission",
    "bind_audit_gates",
    "engine_turn_hooks",
]
