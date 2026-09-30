"""按战绩规划的工作集生效 —— 提拔 / 逐出 / 隔离作用到内核路径（skill-working-set，ADR 0090）。

影子模式（``fitness_shadow``）只算分、只记录。``SkillWorkingSet`` 是生效的那一个：内核在每条
战绩落定后把它交给 ``observe``，并在三个位置读它的结论：

| 位置 | 读什么 | 效果 |
| --- | --- | --- |
| system prompt 的 child 块 | ``promoted`` | 召回模式下把工作集里的 child 直接列出，免搜索 |
| child 列表与召回池 | ``hidden`` | 被隔离的 skill 不再出现 |
| ``call_skill`` 派发 | ``blocked`` | 被隔离的 skill 拒绝派发（仅 ``quarantine_effect="block"``） |

结论在 turn 开始时取一次快照（``WorkingSetView``），整 turn 不变：system prompt 不在 turn 中途
改变。战绩由别的会话写入后，下一 turn 才看得到。

状态不落盘：工作集与隔离集只由存储里的战绩聚合与策略决定，重启后重算得到同样的结果。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args

from taifeng.skill.fitness import SkillFitnessLedger
from taifeng.skill.trust import TRUST_TIERS, tier_of
from taifeng.skill.working_set import (
    FitnessScorer,
    SkillFitnessScore,
    WilsonFitnessScorer,
    WorkingSetPlan,
    WorkingSetPolicy,
    plan_working_set,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from taifeng.skill.outcome import SkillExecutionRecord
    from taifeng.skill.registry import SkillSnapshot
    from taifeng.skill.trust import SkillTrustPolicy, TrustTier

logger = logging.getLogger(__name__)

QuarantineEffect = Literal["flag", "hide", "block"]
"""隔离的作用范围：

- ``flag``：只打事件，不改变可见性与派发；
- ``hide``：对模型隐藏（child 列表、召回池都不再出现），按 id 直接派发仍可；
- ``block``：隐藏，且拒绝派发。
"""

WorkingSetChangeKind = Literal["promoted", "evicted", "quarantined", "released"]


@dataclass(frozen=True)
class WorkingSetView:
    """工作集结论在某一时刻的快照，供一个 turn 使用。

    Attributes:
        promoted: 工作集，按战绩分从高到低。
        hidden: 对模型隐藏的 skill。
        blocked: 拒绝派发的 skill。
    """

    promoted: tuple[str, ...] = ()
    hidden: frozenset[str] = frozenset()
    blocked: frozenset[str] = frozenset()


EMPTY_VIEW = WorkingSetView()

WORKING_SET_VIEW_EXTRAS_KEY = "working_set_view"
"""工具上下文里承载本 turn 工作集快照的键。"""


def view_from_extras(extras: Mapping[str, object]) -> WorkingSetView:
    """取工具上下文里的工作集快照；未启用时为空快照。"""
    view = extras.get(WORKING_SET_VIEW_EXTRAS_KEY)
    return view if isinstance(view, WorkingSetView) else EMPTY_VIEW


@dataclass(frozen=True)
class WorkingSetChange:
    """一次生效的变更。

    Attributes:
        kind: 变更类型。
        skill_id: 发生变更的 skill。
        score: 该 skill 当前的战绩分；已没有战绩记录时为 None。
        trust_tier: 该 skill 的来源信任层级；未知为 None。
        trigger_call_id: 触发这次重算的执行；启动时重算产生的变更为 None。
    """

    kind: WorkingSetChangeKind
    skill_id: str
    score: SkillFitnessScore | None
    trust_tier: TrustTier | None
    trigger_call_id: str | None

    def as_payload(self) -> dict[str, object]:
        """事件 data。"""
        score = self.score
        return {
            "skill_id": self.skill_id,
            "score": None if score is None else score.score,
            "success_rate": None if score is None else score.success_rate,
            "decided_samples": None if score is None else score.decided_samples,
            "trust_tier": self.trust_tier,
            "trigger_call_id": self.trigger_call_id,
        }


@dataclass
class SkillWorkingSet:
    """生效的工作集：记战绩、重算规划、把结论交给内核读。

    同一个实例可被一个 pool 里的全部会话共用（战绩本来就是跨会话的）；写入串行化。

    Attributes:
        store: 战绩聚合存储，须同时支持写入与遍历。
        policy: 工作集策略。
        scorer: 算分口径；缺省 ``WilsonFitnessScorer()``。
        quarantine_effect: 隔离的作用范围，缺省 ``hide``。

    Raises:
        TypeError: 存储缺遍历能力。
        ValueError: ``quarantine_effect`` 取值非法。
    """

    store: SkillFitnessLedger
    policy: WorkingSetPolicy
    scorer: FitnessScorer = field(default_factory=WilsonFitnessScorer)
    quarantine_effect: QuarantineEffect = "hide"
    _promoted: tuple[str, ...] = field(default=(), init=False)
    _quarantined: frozenset[str] = field(default=frozenset(), init=False)
    _tiers: dict[str, TrustTier] = field(default_factory=dict, init=False)
    _restored: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        """构造期校验（缺能力 / 非法取值显式报错，不等到首条战绩到达才失败）。"""
        if not isinstance(self.store, SkillFitnessLedger):
            raise TypeError(
                "store must implement record / fitness / all_fitness "
                "(SkillFitnessStore + SkillFitnessCatalog)"
            )
        if self.quarantine_effect not in get_args(QuarantineEffect):
            raise ValueError(
                f"quarantine_effect must be one of {get_args(QuarantineEffect)}, "
                f"got {self.quarantine_effect!r}"
            )

    @property
    def promoted(self) -> tuple[str, ...]:
        """当前工作集，按战绩分从高到低。"""
        return self._promoted

    @property
    def quarantined(self) -> frozenset[str]:
        """当前隔离集。"""
        return self._quarantined

    def view(self) -> WorkingSetView:
        """当前结论的快照。"""
        hides = self.quarantine_effect in ("hide", "block")
        return WorkingSetView(
            promoted=self._promoted,
            hidden=self._quarantined if hides else frozenset(),
            blocked=self._quarantined if self.quarantine_effect == "block" else frozenset(),
        )

    def blocks(self, skill_id: str) -> bool:
        """该 skill 此刻是否被拒绝派发。"""
        return self.quarantine_effect == "block" and skill_id in self._quarantined

    async def restore(
        self, snapshot: SkillSnapshot, trust: SkillTrustPolicy | None,
    ) -> tuple[WorkingSetChange, ...]:
        """由存储里已有的战绩重算一次（进程启动后首次使用前）；已重算过则不再做。

        Args:
            snapshot: 当前注册表快照，用来确定各 skill 的来源信任层级。
            trust: 来源信任策略；None = 层级未知，用通用门槛。
        """
        async with self._lock:
            if self._restored:
                return ()
            self._learn_tiers(snapshot, trust)
            changes = await self._replan(trigger_call_id=None)
            self._restored = True
            return changes

    async def observe(self, record: SkillExecutionRecord) -> tuple[WorkingSetChange, ...]:
        """计入一条战绩并重算；返回生效的变更。该执行已计入过（重复投递）时不重算。"""
        async with self._lock:
            before = await self.store.fitness(record.skill_id)
            await self.store.record(record)
            after = await self.store.fitness(record.skill_id)
            # 存储按 call_id 幂等：计数没变即重复投递
            if after is None or (before is not None and after.total == before.total):
                return ()
            for tier in TRUST_TIERS:
                if record.trust_tier == tier:
                    self._tiers[record.skill_id] = tier
            return await self._replan(trigger_call_id=record.call_id)

    def _learn_tiers(self, snapshot: SkillSnapshot, trust: SkillTrustPolicy | None) -> None:
        """记下注册表里各 skill 的来源信任层级。"""
        for skill in snapshot.skills:
            tier = tier_of(trust, skill)
            if tier is not None:
                self._tiers[skill.id] = tier

    async def _replan(self, *, trigger_call_id: str | None) -> tuple[WorkingSetChange, ...]:
        """按存储里的全部战绩重算并应用；调用方须持有锁。"""
        scores = {
            fitness.skill_id: self.scorer.score(fitness)
            for fitness in await self.store.all_fitness()
        }
        plan = plan_working_set(
            list(scores.values()),
            promoted=frozenset(self._promoted),
            quarantined=self._quarantined,
            policy=self.policy,
            tiers=self._tiers,
        )
        self._promoted = plan.promoted
        self._quarantined = frozenset(plan.quarantined)
        changes = self._changes(plan, scores, trigger_call_id)
        for change in changes:
            logger.info(
                "skill working set: %s skill=%s score=%s tier=%s",
                change.kind, change.skill_id,
                None if change.score is None else round(change.score.score, 4),
                change.trust_tier,
            )
        return changes

    def _changes(
        self,
        plan: WorkingSetPlan,
        scores: dict[str, SkillFitnessScore],
        trigger_call_id: str | None,
    ) -> tuple[WorkingSetChange, ...]:
        """把规划里的四类变更展开成逐条记录；隔离与解除在前，逐出与提拔在后。"""
        grouped: tuple[tuple[WorkingSetChangeKind, tuple[str, ...]], ...] = (
            ("quarantined", plan.quarantine),
            ("released", plan.release),
            ("evicted", plan.evict),
            ("promoted", plan.promote),
        )
        return tuple(
            WorkingSetChange(
                kind=kind,
                skill_id=skill_id,
                score=scores.get(skill_id),
                trust_tier=self._tiers.get(skill_id),
                trigger_call_id=trigger_call_id,
            )
            for kind, skill_ids in grouped
            for skill_id in skill_ids
        )


__all__ = [
    "EMPTY_VIEW",
    "WORKING_SET_VIEW_EXTRAS_KEY",
    "QuarantineEffect",
    "SkillWorkingSet",
    "WorkingSetChange",
    "WorkingSetChangeKind",
    "WorkingSetView",
    "view_from_extras",
]
