"""按选择置信度分流 —— 认知回路相位 3：验证（④ 评估 → ⑤ 试用）（skill-selection-gate，ADR 0088）。

相位 2 把召回候选连同置信度交给模型，由模型自己掂量。模型并不总是掂量：置信 0.2 的候选
与 0.9 的候选在它眼里都是「搜到了」。本模块把置信度变成**有约束力的分流**：

| 分流 | 条件（默认策略） | 派发前的要求 |
| --- | --- | --- |
| ``proceed`` | 置信 ≥ ``tau_high``，且与并列候选拉得开 | 无 |
| ``trial`` | 置信落在 [``tau_low``, ``tau_high``)，或与另一个候选难分 | 先试用：模型读过说明书（``read_skill``），或试用门（``TrialJudge``）放行 |
| ``escalate`` | 置信 < ``tau_low`` | 本轮不可派发：换关键词重搜、如实报告没有匹配、或问用户 |

分流只约束**经发现选中**的 skill。作者列在白名单里、模型直接选中的子 skill 不受影响——
那是作者预授权的工作集。

置信度在这里决定的是「要不要先试」，不是「值不值得信」：后者只看真实执行结果
（``working_set``）。长相与战绩分离的不变量不变。

本模块的判定全部由 history 推导（模型看到的那份召回结果、它之后有没有读过说明书），
不在 runner 上持有状态：冷恢复后结论不变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.skill.verify import SkillVerifier

SelectionRoute = Literal["proceed", "trial", "escalate"]
"""一个召回候选的分流结论。"""

ROUTE_FIELD = "route"
"""``search_skills`` 结果里每个候选承载分流结论的键。"""

SEARCH_TOOL_NAME = "search_skills"
READ_TOOL_NAME = "read_skill"


@dataclass(frozen=True)
class SelectionCandidate:
    """参与分流的一个候选：只有 id 与置信度。"""

    skill_id: str
    confidence: float


@dataclass(frozen=True)
class RoutedCandidate:
    """一个候选的分流结论与依据。"""

    skill_id: str
    confidence: float
    route: SelectionRoute
    reason: str


@runtime_checkable
class SelectionConfidencePolicy(Protocol):
    """选择置信度分流协议：内核给默认实现，业务可注入自己的口径。"""

    def route(self, candidates: Sequence[SelectionCandidate]) -> Sequence[RoutedCandidate]:
        """给每个候选一个分流结论；SHALL 逐个返回、顺序不变。"""
        ...


@dataclass(frozen=True)
class ThresholdSelectionPolicy:
    """默认分流：两个阈值加一个并列间距。

    Attributes:
        tau_high: 置信 ≥ 此值才可能直接派发。
        tau_low: 置信 < 此值一律升级。
        ambiguity_margin: 置信最高的候选与另一个候选的差 < 此值时两者难分，都须先试用；
            0 = 不做并列判定。

    Raises:
        ValueError: 阈值不在 [0, 1] 内、``tau_low > tau_high``、或间距为负。
    """

    tau_high: float = 0.75
    tau_low: float = 0.4
    ambiguity_margin: float = 0.05

    def __post_init__(self) -> None:
        """构造期校验。"""
        for name in ("tau_high", "tau_low"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1], got {value!r}")
        if self.tau_low > self.tau_high:
            raise ValueError(
                f"tau_low ({self.tau_low}) must not exceed tau_high ({self.tau_high})"
            )
        if self.ambiguity_margin < 0:
            raise ValueError(
                f"ambiguity_margin must be non-negative, got {self.ambiguity_margin!r}"
            )

    def route(self, candidates: Sequence[SelectionCandidate]) -> tuple[RoutedCandidate, ...]:
        """按阈值与并列间距分流。"""
        contested = self._contested(candidates)
        return tuple(self._route_one(candidate, contested) for candidate in candidates)

    def _contested(self, candidates: Sequence[SelectionCandidate]) -> frozenset[str]:
        """与置信最高者难分的候选（含最高者自己）；没有并列时为空。"""
        eligible = [c for c in candidates if c.confidence >= self.tau_high]
        if self.ambiguity_margin <= 0 or len(eligible) < 2:
            return frozenset()
        top = max(c.confidence for c in eligible)
        close = [c.skill_id for c in eligible if top - c.confidence < self.ambiguity_margin]
        return frozenset(close) if len(close) > 1 else frozenset()

    def _route_one(
        self, candidate: SelectionCandidate, contested: frozenset[str],
    ) -> RoutedCandidate:
        """单个候选的分流。"""
        confidence = candidate.confidence
        if confidence < self.tau_low:
            route: SelectionRoute = "escalate"
            reason = f"confidence {confidence:.2f} is below {self.tau_low:.2f}"
        elif confidence < self.tau_high:
            route = "trial"
            reason = f"confidence {confidence:.2f} is below {self.tau_high:.2f}"
        elif candidate.skill_id in contested:
            route = "trial"
            reason = "too close to another candidate to tell apart"
        else:
            route = "proceed"
            reason = f"confidence {confidence:.2f} is at least {self.tau_high:.2f}"
        return RoutedCandidate(candidate.skill_id, confidence, route, reason)


@dataclass(frozen=True)
class TrialVerdict:
    """试用门的结论。"""

    approved: bool
    reason: str


@runtime_checkable
class TrialJudge(Protocol):
    """试用门：读完整说明书后判断这个 skill 能不能用于当前任务。"""

    async def judge(
        self, *, task: str, skill_id: str, description: str, body: str,
        cancel: CancellationToken,
    ) -> TrialVerdict:
        """给出放行 / 拒绝。实现 MUST 可取消（R4）。"""
        ...


class VerifierTrialJudge:
    """用既有的 ``SkillVerifier``（输入要求适配精验）充当试用门。"""

    def __init__(self, verifier: SkillVerifier) -> None:
        """
        Args:
            verifier: 召回后验证门用的同一类后端（如 ``LlmSkillVerifier``）。
        """
        self._verifier = verifier

    async def judge(
        self, *, task: str, skill_id: str, description: str, body: str,
        cancel: CancellationToken,
    ) -> TrialVerdict:
        """对单个候选做一次适配精验；验证后端只返回适用的候选。"""
        from taifeng.skill.recall import SkillCandidate

        candidate = SkillCandidate(
            skill_id=skill_id, description=description, score=0.0, confidence=0.0,
            matched_snippet=None,
        )
        verified = await self._verifier.verify(
            task, [candidate], get_body=lambda _: body, cancel=cancel
        )
        for item in verified:
            if item.skill_id == skill_id:
                return TrialVerdict(approved=True, reason=item.reason)
        return TrialVerdict(approved=False, reason="input requirements are not met")


@dataclass(frozen=True)
class SkillSelectionGate:
    """相位 3 的注入件：分流策略 + 可选的试用门。

    Attributes:
        policy: 分流策略。
        trial_judge: 试用门；None = ``trial`` 档要求模型自己先 ``read_skill``。
    """

    policy: SelectionConfidencePolicy
    trial_judge: TrialJudge | None = None


# ------------------------------------------------------------------
# 由 history 推导
# ------------------------------------------------------------------


@dataclass(frozen=True)
class RecalledSelection:
    """模型最近一次看到某个 skill 被召回时的分流结论。"""

    skill_id: str
    confidence: float | None
    route: SelectionRoute
    search_index: int
    """那条召回结果在 history 里的下标。"""


def _turn_start(history: Sequence[ResponseItem]) -> int:
    """当前 turn 在 history 里的起点（最后一条用户消息的下标；没有则为 0）。"""
    for index in range(len(history) - 1, -1, -1):
        if history[index].kind == "user_message":
            return index
    return 0


def _call_names(history: Sequence[ResponseItem], start: int) -> dict[str, tuple[str, str]]:
    """``start`` 之后各工具调用的 call_id → (工具名, 原始参数)。"""
    return {
        str(item.payload.get("call_id")): (
            str(item.payload.get("name")), str(item.payload.get("arguments") or "")
        )
        for item in history[start:]
        if item.kind in ("function_call", "tool_intent")
    }


def _routes_in(output: str) -> dict[str, tuple[SelectionRoute, float | None]]:
    """从一条 ``search_skills`` 结果里读出各候选的分流；结果不带分流信息时为空。"""
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return {}
    entries = parsed.get("low_confidence", []) if isinstance(parsed, dict) else parsed
    if not isinstance(entries, list):
        return {}
    routes: dict[str, tuple[SelectionRoute, float | None]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        skill_id, route = entry.get("skill_id"), entry.get(ROUTE_FIELD)
        if not isinstance(skill_id, str) or route not in ("proceed", "trial", "escalate"):
            continue
        confidence = entry.get("confidence")
        routes[skill_id] = (
            route,
            float(confidence) if isinstance(confidence, (int, float)) else None,
        )
    return routes


def latest_selection(
    history: Sequence[ResponseItem], skill_id: str,
) -> RecalledSelection | None:
    """当前 turn 里模型最近一次看到该 skill 被召回时的分流；没有被召回过返回 None。

    跨 turn 的召回不算：模型在新一轮里的选择依据是新一轮的上下文。
    """
    start = _turn_start(history)
    names = _call_names(history, start)
    for index in range(len(history) - 1, start - 1, -1):
        item = history[index]
        if item.kind != "function_call_output" or item.payload.get("is_error"):
            continue
        name, _ = names.get(str(item.payload.get("call_id")), ("", ""))
        if name != SEARCH_TOOL_NAME:
            continue
        found = _routes_in(str(item.payload.get("output") or "")).get(skill_id)
        if found is not None:
            return RecalledSelection(skill_id, found[1], found[0], index)
    return None


def was_read_after(
    history: Sequence[ResponseItem], skill_id: str, after_index: int,
) -> bool:
    """``after_index`` 之后模型是否成功读过该 skill 的说明书（``read_skill``）。"""
    names = _call_names(history, after_index)
    for item in history[after_index + 1:]:
        if item.kind != "function_call_output" or item.payload.get("is_error"):
            continue
        name, arguments = names.get(str(item.payload.get("call_id")), ("", ""))
        if name != READ_TOOL_NAME:
            continue
        try:
            parsed = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("skill_id") == skill_id:
            return True
    return False


__all__ = [
    "READ_TOOL_NAME",
    "ROUTE_FIELD",
    "SEARCH_TOOL_NAME",
    "RecalledSelection",
    "RoutedCandidate",
    "SelectionCandidate",
    "SelectionConfidencePolicy",
    "SelectionRoute",
    "SkillSelectionGate",
    "ThresholdSelectionPolicy",
    "TrialJudge",
    "TrialVerdict",
    "VerifierTrialJudge",
    "latest_selection",
    "was_read_after",
]
