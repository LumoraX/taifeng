"""skill-to-skill 派发：CallStack、DispatchPolicy、静态 / 动态环检测。

按 ADR 0006，composite skill 可通过 ``call_skill`` 调用子 skill。
派发必须经过两层保护：

1. 启动期：``detect_cycles`` 静态环检测；任一环 → 拒绝启动
2. 运行期：``DispatchPolicy.check`` 校验深度、白名单、动态环

参照：docs/architecture/skill-system.md
ADR：docs/decisions/0006-unified-skill-model.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from taifeng.permission.types import (
        PermissionDecision,
        PermissionPolicy,
        PermissionRequest,
    )
    from taifeng.skill.definition import SkillDefinition


SubagentApprovalMode = Literal["inherit", "auto_deny", "auto_allow"]
"""子 skill 派发时如何处理 PermissionPolicy ``ask`` 决策（G3）。

- ``inherit`` (默认)：子 turn 完全继承父 policy；``ask`` 时仍弹 prompter
- ``auto_deny``：子 turn 内 ``ask`` 自动转 deny，不调 prompter（无人值守 / 保守默认）
- ``auto_allow``：子 turn 内 ``ask`` 自动转 allow（dev / 高信任环境）
"""


# ---------------------------------------------------------------------------
# 调用栈
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallFrame:
    """单次 call_skill 派发的栈帧。"""
    skill_id: str
    call_id: str
    started_at: datetime
    parent_call_id: str | None = None


@dataclass(frozen=True)
class CallStack:
    """skill 调用栈。frames[0] 是 entry skill。

    用 immutable tuple 保证跨 turn 共享安全。``push`` 返回新栈，不可变。
    """
    frames: tuple[CallFrame, ...] = ()

    @property
    def depth(self) -> int:
        return len(self.frames)

    @property
    def current(self) -> CallFrame | None:
        return self.frames[-1] if self.frames else None

    def contains(self, skill_id: str) -> bool:
        """动态环检测：目标 skill 是否已在调用栈上。"""
        return any(f.skill_id == skill_id for f in self.frames)

    def path(self) -> list[str]:
        return [f.skill_id for f in self.frames]

    def push(self, skill_id: str, call_id: str) -> CallStack:
        parent = self.frames[-1].call_id if self.frames else None
        frame = CallFrame(
            skill_id=skill_id,
            call_id=call_id,
            started_at=datetime.now(UTC),
            parent_call_id=parent,
        )
        return CallStack(frames=self.frames + (frame,))


# ---------------------------------------------------------------------------
# 派发裁决
# ---------------------------------------------------------------------------


DispatchReason = str  # "max_depth_exceeded" / "cycle_detected" / "not_in_whitelist" / "cannot_call_entry_skill" / "unknown_skill"


@dataclass(frozen=True)
class DispatchVerdict:
    allowed: bool
    reason: DispatchReason | None = None
    path: tuple[str, ...] = ()

    @classmethod
    def allow(cls) -> DispatchVerdict:
        return cls(allowed=True)

    @classmethod
    def reject(cls, reason: DispatchReason, path: list[str] | None = None) -> DispatchVerdict:
        return cls(allowed=False, reason=reason, path=tuple(path or ()))


# ---------------------------------------------------------------------------
# 派发策略
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchPolicy:
    """每次 call_skill 派发前的策略检查。

    四层校验顺序：
        1. 目标 skill 存在
        2. 深度 < max_call_depth
        3. 不在调用栈（动态环检测）
        4. 在 caller 的 child_skills 白名单
        5. 目标 entry == false（entry skill 不可作为子调用）

    G3 subagent-isolation-policy：``subagent_approval_mode`` 控制子 turn 派发时
    如何处理父 ``PermissionPolicy.default_mode='ask'``：

    - ``inherit``（默认）：子 turn 复用同一 policy（含 prompter）；既有行为
    - ``auto_deny``：子 turn 用 ``_SubagentAutoDecisionPolicy`` 包装，``ask``
      自动转 deny（不调 prompter）。无人值守 / 保守默认
    - ``auto_allow``：同上，``ask`` 自动转 allow（dev / 高信任环境）

    详见 spec ``skill-dispatch`` Requirement "DispatchPolicy 暴露
    subagent_approval_mode 字段"。
    """

    subagent_approval_mode: SubagentApprovalMode = "inherit"
    """子 skill 派发时 PermissionPolicy ``ask`` 的处理策略。"""

    def __post_init__(self) -> None:
        if self.subagent_approval_mode not in (
            "inherit", "auto_deny", "auto_allow",
        ):
            raise ValueError(
                "subagent_approval_mode must be one of "
                "{'inherit', 'auto_deny', 'auto_allow'}, "
                f"got {self.subagent_approval_mode!r}"
            )

    def check(
        self,
        stack: CallStack,
        caller: SkillDefinition,
        target: SkillDefinition | None,
        *,
        allow_entry_target: bool = False,
    ) -> DispatchVerdict:
        """派发准入裁决。

        ``allow_entry_target``（detached spawn 专用）：``call_skill`` 是把目标作为
        **嵌套子调用**阻塞父 turn，调 entry skill 语义错误故默认拒绝；而 ``spawn_skill``
        是把目标作为**独立 child thread**分离发起（等价于另起一个根），调 entry skill
        恰是其正当用法（与 ``set_join_barrier`` 的 ``then_skill`` 已豁免 entry 同理）。
        故 spawn 路径传 ``True`` 跳过「不可调 entry」门，其余四层（存在 / 深度 / 环 /
        白名单）仍照常裁决。
        """
        if target is None:
            return DispatchVerdict.reject("unknown_skill", stack.path())

        # caller 的 max_call_depth 决定调用图的深度上限
        max_depth = caller.max_call_depth
        if stack.depth >= max_depth:
            return DispatchVerdict.reject("max_depth_exceeded", stack.path())

        if stack.contains(target.id):
            return DispatchVerdict.reject(
                "cycle_detected", stack.path() + [target.id]
            )

        if target.id not in caller.child_skills:
            return DispatchVerdict.reject(
                "not_in_whitelist", [caller.id, target.id]
            )

        if target.entry and not allow_entry_target:
            return DispatchVerdict.reject(
                "cannot_call_entry_skill", [caller.id, target.id]
            )

        return DispatchVerdict.allow()


# ---------------------------------------------------------------------------
# G3: 子 turn PermissionPolicy 包装类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SubagentAutoDecisionPolicy:
    """子 turn 内的 PermissionPolicy 包装 —— ``ask`` 决策走固定 fallback。

    业务侧不直接构造；由 ``TurnRunner.run_sub_skill`` 根据父
    ``DispatchPolicy.subagent_approval_mode`` 在 ``auto_deny`` / ``auto_allow``
    模式下创建。

    行为（spec ``skill-dispatch`` Requirement
    "_SubagentAutoDecisionPolicy 复用 inner rules 但跳过 prompter"）：

    1. 复用 ``inner.rules``：命中 ``allow`` / ``deny`` 走 rule 决策
    2. 命中 ``ask`` **或** ``inner.default_mode == 'ask'`` 时走 fallback
    3. 否则按 ``inner.default_mode`` 直接 allow / deny
    4. 永不调用 ``inner.prompter``
    """

    inner: PermissionPolicy
    fallback: Literal["allow", "deny"]

    async def check(self, request: PermissionRequest) -> PermissionDecision:
        from taifeng.permission.types import PermissionDecision as _Decision

        # 查找首个命中 rule
        for rule in self.inner.rules:
            if rule.matches(request):
                mode = rule.mode
                rule_reason = rule.reason or (
                    f"matched rule: {rule.target_pattern}"
                )
                if mode == "allow":
                    return _Decision.allow(
                        reason=rule_reason or "subagent_rule_allow",
                    )
                if mode == "deny":
                    return _Decision.deny(
                        reason=rule_reason or "subagent_rule_deny",
                    )
                # mode == 'ask' → 走 fallback
                return self._fallback_decision()

        # 全不命中 → 看 inner.default_mode
        default = self.inner.default_mode
        if default == "allow":
            return _Decision.allow(reason="inner_default_allow")
        if default == "deny":
            return _Decision.deny(reason="inner_default_deny")
        # default == 'ask' → 走 fallback
        return self._fallback_decision()

    def _fallback_decision(self) -> PermissionDecision:
        from taifeng.permission.types import PermissionDecision as _Decision

        if self.fallback == "allow":
            return _Decision.allow(reason="subagent_auto_allow")
        return _Decision.deny(reason="subagent_auto_deny")


# ---------------------------------------------------------------------------
# 静态环检测（启动期）
# ---------------------------------------------------------------------------


class CircularSkillReference(Exception):
    """skill 调用图存在环，拒绝启动。"""


def detect_cycles(skills: Mapping[str, SkillDefinition]) -> list[list[str]]:
    """DFS 三色标记检测有向图中的所有环。

    Args:
        skills: skill_id → SkillDefinition 的映射

    Returns:
        list of cycle paths；每个 path 是组成环的 skill_id 序列（含闭合点）

    示例：
        >>> # a → b → c → a, d → d
        >>> detect_cycles({...})
        [['a', 'b', 'c', 'a'], ['d', 'd']]
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(skills, WHITE)
    cycles: list[list[str]] = []
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        defn = skills[node]
        for child_id in defn.child_skills:
            if child_id not in skills:
                # 未知子 skill —— 由上层 UnknownChildSkill 处理；环检测忽略
                continue
            if color[child_id] == GRAY:
                # 找到环
                start_idx = stack.index(child_id)
                cycles.append(stack[start_idx:] + [child_id])
            elif color[child_id] == WHITE:
                dfs(child_id)
        stack.pop()
        color[node] = BLACK

    for sid in skills:
        if color[sid] == WHITE:
            dfs(sid)

    return cycles
