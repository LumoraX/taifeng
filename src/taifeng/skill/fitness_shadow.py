"""战绩算分的影子模式 —— 只算分、只记录、不生效（skill-working-set，ADR 0077）。

``SkillFitnessShadow`` 是一个 ``TelemetrySink``：每收到一条 ``skill_outcome_recorded``，把战绩
计入存储，重算全部 skill 的战绩分与工作集规划，把「如果生效会发生什么」交给 observer。

**不生效是结构性保证，不是开关**：本组件只经事件流旁路挂接（``attach(engine)``），内核的
prompt 组装、召回、派发路径都拿不到它的引用，也就不可能读它的结论。上线步骤因此是：
先挂影子积累数据、核对结论可靠，再另行启用生效路径。

影子状态（假想的工作集与隔离集）只存在于本组件内存中，随进程消失；重启后由存储里的聚合
重算得到同样的目标状态，只是首轮评估会把它们全部报告为新变更。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from taifeng.skill.fitness import SkillFitnessLedger
from taifeng.skill.outcome import SkillExecutionRecord
from taifeng.skill.working_set import (
    FitnessScorer,
    SkillFitnessScore,
    WilsonFitnessScorer,
    WorkingSetPlan,
    WorkingSetPolicy,
    plan_working_set,
)

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.event import EventMsg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowEvaluation:
    """一次影子评估的结论。

    Attributes:
        skill_id: 触发本次评估的战绩所属 skill。
        call_id: 触发本次评估的执行。
        score: 该 skill 计入本条战绩后的战绩分。
        plan: 全部 skill 的工作集规划；``promote`` / ``evict`` / ``quarantine`` / ``release``
            是相对上一次评估的假想状态的变更。
        shadow: 恒为 True——结论未生效。
    """

    skill_id: str
    call_id: str
    score: SkillFitnessScore
    plan: WorkingSetPlan
    shadow: bool = True


@runtime_checkable
class ShadowObserver(Protocol):
    """影子评估结论的去向（业务落库 / 打指标）。"""

    async def on_evaluation(self, evaluation: ShadowEvaluation) -> None:
        """接收一次评估结论。"""
        ...


@dataclass
class SkillFitnessShadow:
    """把 ``skill_outcome_recorded`` 事件接到「算分 + 规划 + 记录」的 TelemetrySink。

    其余事件忽略。存储与 observer 抛出的异常原样上抛，由挂接方决定处置。

    Attributes:
        store: 战绩聚合存储，须同时支持写入与遍历。
        policy: 工作集策略。
        scorer: 算分口径；缺省 ``WilsonFitnessScorer()``。
        observer: 结论去向；None = 只写日志。
    """

    store: SkillFitnessLedger
    policy: WorkingSetPolicy
    scorer: FitnessScorer = field(default_factory=WilsonFitnessScorer)
    observer: ShadowObserver | None = None
    _promoted: frozenset[str] = field(default=frozenset(), init=False)
    _quarantined: frozenset[str] = field(default=frozenset(), init=False)

    def __post_init__(self) -> None:
        """构造期校验存储能力（缺能力显式报错，不在首条事件到达时才失败）。"""
        if not isinstance(self.store, SkillFitnessLedger):
            raise TypeError(
                "store must implement record / fitness / all_fitness "
                "(SkillFitnessStore + SkillFitnessCatalog)"
            )

    @property
    def shadow_promoted(self) -> frozenset[str]:
        """假想的工作集（未生效）。"""
        return self._promoted

    @property
    def shadow_quarantined(self) -> frozenset[str]:
        """假想的隔离集（未生效）。"""
        return self._quarantined

    async def handle(self, ev: EventMsg) -> None:
        """处理一条事件：只处理战绩记录。"""
        if ev.msg.kind != "skill_outcome_recorded":
            return
        record = SkillExecutionRecord.from_payload(ev.msg.data)
        evaluation = await self.evaluate(record)
        if evaluation is None:
            return
        logger.info(
            "skill fitness shadow: skill=%s score=%.4f rate=%.4f samples=%d "
            "would_promote=%s would_evict=%s would_quarantine=%s would_release=%s",
            evaluation.skill_id,
            evaluation.score.score,
            evaluation.score.success_rate,
            evaluation.score.decided_samples,
            list(evaluation.plan.promote),
            list(evaluation.plan.evict),
            list(evaluation.plan.quarantine),
            list(evaluation.plan.release),
        )
        if self.observer is not None:
            await self.observer.on_evaluation(evaluation)

    async def evaluate(self, record: SkillExecutionRecord) -> ShadowEvaluation | None:
        """计入一条战绩并重算；该执行已计入过（重复投递）返回 None。"""
        before = await self.store.fitness(record.skill_id)
        await self.store.record(record)
        after = await self.store.fitness(record.skill_id)
        # 存储按 call_id 幂等：计数没变即重复投递，不重复评估
        if after is None or (before is not None and after.total == before.total):
            return None
        scores = [self.scorer.score(fitness) for fitness in await self.store.all_fitness()]
        plan = plan_working_set(
            scores,
            promoted=self._promoted,
            quarantined=self._quarantined,
            policy=self.policy,
        )
        self._promoted = frozenset(plan.promoted)
        self._quarantined = frozenset(plan.quarantined)
        return ShadowEvaluation(
            skill_id=record.skill_id,
            call_id=record.call_id,
            score=self.scorer.score(after),
            plan=plan,
        )

    async def attach(self, engine: AgentEngine) -> None:
        """订阅 engine 全量事件流并持续评估（与 ``SkillFitnessRecorder.attach`` 同形）。"""
        async for ev in engine.subscribe_all():
            await self.handle(ev)


__all__ = [
    "ShadowEvaluation",
    "ShadowObserver",
    "SkillFitnessShadow",
]
