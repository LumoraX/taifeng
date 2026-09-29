"""按战绩算分与工作集规划 —— 认知回路 ⑦ 沉淀相位的决策部分（skill-working-set，ADR 0077）。

输入是 ``SkillFitness``（真实执行结果的聚合），输出是「哪些 skill 值得常驻工作记忆、哪些该被
隔离」的规划。本模块全是纯函数与不可变值：不做 IO、不读时钟、不持有状态，同一输入恒得同一
输出。规划是否生效由调用方决定（影子模式只记录，见 ``fitness_shadow``）。

三条不变量：

1. **长相不得喂战绩**：算分只读成败计数与成本，任何路径都不读选择置信度。描述写得好的假
   skill 会被高置信选中，若置信度能抬分，漏洞就被焊进了系统。
2. **没真干成过就不涨分**：没有成败样本的 skill 得 0 分；样本少时分数被压低（Wilson 下界）。
3. **放弃不算失败**：取消、人拒绝等放弃终态不是 skill 的成败，不进成功率分母。

参照：认知回路设计稿 §6 相位 5。差异：不引入独立的 LRU / 频次项——Wilson 下界随样本数
收紧，「用得多且干得成」已经由它一并表达。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from taifeng.skill.fitness import SkillFitness
    from taifeng.skill.trust import TrustTier


@dataclass(frozen=True)
class SkillFitnessScore:
    """单个 skill 的战绩分。

    Attributes:
        skill_id: skill 标识。
        score: 综合分，[0, 1]；排序与提拔阈值都用它。
        success_rate: 成功率点估计 = successes / decided；无成败样本为 0。
        success_lower_bound: 成功率的置信下界（未计成本）。
        decided_samples: 分出成败的执行次数。
        total_samples: 全部终态执行次数（含放弃），即被选中并跑到终态的次数。
        mean_cost_tokens: 平均每次执行消耗的 token。
    """

    skill_id: str
    score: float
    success_rate: float
    success_lower_bound: float
    decided_samples: int
    total_samples: int
    mean_cost_tokens: float


@runtime_checkable
class FitnessScorer(Protocol):
    """战绩算分协议：内核给默认实现，业务可注入自己的口径。"""

    def score(self, fitness: SkillFitness) -> SkillFitnessScore:
        """由聚合算出战绩分。实现 MUST NOT 读取任何选择置信度。"""
        ...


@dataclass(frozen=True)
class WilsonFitnessScorer:
    """默认算分：成功率的 Wilson 置信下界，可选按成本折减。

    Attributes:
        z: 置信水平对应的正态分位数（1.96 ≈ 95%）；越大对小样本越保守。
        cost_scale_tokens: 成本折减的尺度；None = 不计成本。配置后
            ``score = 下界 / (1 + 平均 token / cost_scale_tokens)``，平均成本等于该尺度时分数减半。
    """

    z: float = 1.96
    cost_scale_tokens: int | None = None

    def __post_init__(self) -> None:
        """构造期校验（非法值显式报错）。"""
        if not self.z > 0:
            raise ValueError(f"z must be positive, got {self.z!r}")
        if self.cost_scale_tokens is not None and self.cost_scale_tokens <= 0:
            raise ValueError(
                f"cost_scale_tokens must be positive, got {self.cost_scale_tokens!r}"
            )

    def score(self, fitness: SkillFitness) -> SkillFitnessScore:
        """算出战绩分。"""
        decided = fitness.decided
        total = fitness.total
        mean_cost = fitness.cost_tokens_total / total if total else 0.0
        if decided == 0:
            return SkillFitnessScore(
                skill_id=fitness.skill_id,
                score=0.0,
                success_rate=0.0,
                success_lower_bound=0.0,
                decided_samples=0,
                total_samples=total,
                mean_cost_tokens=mean_cost,
            )
        rate = fitness.successes / decided
        bound = _wilson_lower_bound(rate, decided, self.z)
        score = bound
        if self.cost_scale_tokens is not None:
            score = bound / (1.0 + mean_cost / self.cost_scale_tokens)
        return SkillFitnessScore(
            skill_id=fitness.skill_id,
            score=score,
            success_rate=rate,
            success_lower_bound=bound,
            decided_samples=decided,
            total_samples=total,
            mean_cost_tokens=mean_cost,
        )


def _wilson_lower_bound(rate: float, samples: int, z: float) -> float:
    """二项比例的 Wilson score 区间下界，夹到 [0, 1]。"""
    z2 = z * z
    centre = rate + z2 / (2 * samples)
    margin = z * math.sqrt((rate * (1 - rate) + z2 / (4 * samples)) / samples)
    return min(1.0, max(0.0, (centre - margin) / (1 + z2 / samples)))


@dataclass(frozen=True)
class TierRule:
    """某个来源信任层级上对通用策略的调整（ADR 0090）。

    Attributes:
        promotable: 该层级的 skill 能否被提拔进工作集。
        promote_min_samples: 该层级提拔所需的最少成败样本数；None = 沿用通用值。
        quarantine_min_samples: 该层级判隔离所需的最少成败样本数；None = 沿用通用值。
        quarantine_exempt: 该层级的 skill 不被隔离（战绩再差也只是不提拔）。
    """

    promotable: bool = True
    promote_min_samples: int | None = None
    quarantine_min_samples: int | None = None
    quarantine_exempt: bool = False

    def __post_init__(self) -> None:
        """构造期校验。"""
        for name in ("promote_min_samples", "quarantine_min_samples"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1, got {value!r}")


_NEUTRAL_RULE = TierRule()


@dataclass(frozen=True)
class WorkingSetPolicy:
    """工作集规划的策略参数（全部由业务注入，内核不含默认业务取值以外的假设）。

    Attributes:
        budget: 常驻工作集最多容纳的 skill 数；0 = 不提拔任何 skill。
        promote_min_score: 提拔所需的最低战绩分。
        promote_min_samples: 提拔所需的最少成败样本数。
        quarantine_min_samples: 判隔离所需的最少成败样本数（样本太少不下结论）。
        quarantine_max_success_rate: 成功率不高于该值、且样本数达标时隔离。
        tier_rules: 按来源信任层级的调整；没有列出的层级、以及层级未知的 skill 用通用值。
    """

    budget: int
    promote_min_score: float = 0.6
    promote_min_samples: int = 5
    quarantine_min_samples: int = 5
    quarantine_max_success_rate: float = 0.2
    tier_rules: Mapping[TrustTier, TierRule] = field(default_factory=dict)

    def rule_for(self, tier: TrustTier | None) -> TierRule:
        """该层级适用的调整；层级未知或未配置时为不作调整。"""
        if tier is None:
            return _NEUTRAL_RULE
        return self.tier_rules.get(tier, _NEUTRAL_RULE)

    def __post_init__(self) -> None:
        """构造期校验（非法值显式报错）。"""
        if self.budget < 0:
            raise ValueError(f"budget must be non-negative, got {self.budget!r}")
        for name in ("promote_min_score", "quarantine_max_success_rate"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1], got {value!r}")
        for name in ("promote_min_samples", "quarantine_min_samples"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be at least 1, got {value!r}")


@dataclass(frozen=True)
class WorkingSetPlan:
    """一次规划的结果：目标状态 + 相对当前状态的变更。

    Attributes:
        promoted: 目标工作集，按战绩分从高到低。
        quarantined: 目标隔离集，按 skill_id 排序。
        promote: 需新提拔的 skill（目标工作集的顺序）。
        evict: 需逐出工作集的 skill（按 skill_id 排序）。
        quarantine: 需新隔离的 skill（按 skill_id 排序）。
        release: 需解除隔离的 skill（按 skill_id 排序）。
    """

    promoted: tuple[str, ...]
    quarantined: tuple[str, ...]
    promote: tuple[str, ...]
    evict: tuple[str, ...]
    quarantine: tuple[str, ...]
    release: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """相对当前状态是否有任何变更。"""
        return bool(self.promote or self.evict or self.quarantine or self.release)


def _should_quarantine(
    score: SkillFitnessScore, policy: WorkingSetPolicy, rule: TierRule,
) -> bool:
    """高选中、低成功：样本数达标且成功率不高于阈值；豁免层级不隔离。"""
    if rule.quarantine_exempt:
        return False
    min_samples = rule.quarantine_min_samples or policy.quarantine_min_samples
    return (
        score.decided_samples >= min_samples
        and score.success_rate <= policy.quarantine_max_success_rate
    )


def _promotable(score: SkillFitnessScore, policy: WorkingSetPolicy, rule: TierRule) -> bool:
    """样本数与战绩分都达到提拔线，且所在层级允许提拔。"""
    if not rule.promotable:
        return False
    min_samples = rule.promote_min_samples or policy.promote_min_samples
    return score.decided_samples >= min_samples and score.score >= policy.promote_min_score


def plan_working_set(
    scores: Sequence[SkillFitnessScore],
    *,
    promoted: frozenset[str],
    policy: WorkingSetPolicy,
    quarantined: frozenset[str] = frozenset(),
    tiers: Mapping[str, TrustTier] | None = None,
) -> WorkingSetPlan:
    """由全部 skill 的战绩分规划工作集与隔离集。

    规划是**无状态重算**：目标状态只由 ``scores`` 与 ``policy`` 决定，``promoted`` /
    ``quarantined`` 只用于算出变更。因此：

    - 超预算时留下战绩分最高的，被挤掉的是最低分者（不是最早进入者）；
    - 已在工作集里的 skill 掉到提拔线以下、被隔离、或已没有战绩记录，都会被逐出；
    - 隔离集里的 skill 不再满足隔离条件（战绩被重置或好转）即解除。

    同分按成败样本数多者优先，再按 skill_id 升序。

    ``tiers``（skill id → 来源信任层级）给出时，每个 skill 按所在层级的 ``TierRule`` 调整
    提拔与隔离的门槛；没有列出的 skill 层级未知，用通用值。层级只调门槛，不改战绩分。

    Raises:
        ValueError: ``scores`` 里同一 skill 出现多次。
    """
    ids = [score.skill_id for score in scores]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate skill id in scores")
    known = tiers or {}
    rules = {score.skill_id: policy.rule_for(known.get(score.skill_id)) for score in scores}
    isolated = {
        score.skill_id for score in scores
        if _should_quarantine(score, policy, rules[score.skill_id])
    }
    candidates = sorted(
        (
            score for score in scores
            if score.skill_id not in isolated
            and _promotable(score, policy, rules[score.skill_id])
        ),
        key=lambda score: (-score.score, -score.decided_samples, score.skill_id),
    )
    target = tuple(score.skill_id for score in candidates[: policy.budget])
    return WorkingSetPlan(
        promoted=target,
        quarantined=tuple(sorted(isolated)),
        promote=tuple(skill_id for skill_id in target if skill_id not in promoted),
        evict=tuple(sorted(promoted - set(target))),
        quarantine=tuple(sorted(isolated - quarantined)),
        release=tuple(sorted(quarantined - isolated)),
    )


__all__ = [
    "FitnessScorer",
    "SkillFitnessScore",
    "TierRule",
    "WilsonFitnessScorer",
    "WorkingSetPlan",
    "WorkingSetPolicy",
    "plan_working_set",
]
