"""skill-working-set —— 按战绩算分与工作集规划（纯函数，ADR 0077）。"""

from __future__ import annotations

import math

import pytest

from taifeng.skill.fitness import SkillFitness
from taifeng.skill.working_set import (
    SkillFitnessScore,
    WilsonFitnessScorer,
    WorkingSetPolicy,
    plan_working_set,
)


def _fitness(
    skill_id: str,
    successes: int = 0,
    failures: int = 0,
    abandoned: int = 0,
    tokens: int = 0,
) -> SkillFitness:
    return SkillFitness(
        skill_id=skill_id, successes=successes, failures=failures, abandoned=abandoned,
        cost_tokens_total=tokens,
    )


def _score(skill_id: str, score: float, samples: int = 10, rate: float = 1.0) -> SkillFitnessScore:
    return SkillFitnessScore(
        skill_id=skill_id, score=score, success_rate=rate, success_lower_bound=score,
        decided_samples=samples, total_samples=samples, mean_cost_tokens=0.0,
    )


# ------------------------------------------------------------------
# 算分
# ------------------------------------------------------------------


def test_no_decided_samples_scores_zero() -> None:
    """没有成败样本（只有放弃）时不给分：没真干成过就不涨分。"""
    score = WilsonFitnessScorer().score(_fitness("s", abandoned=7))
    assert (score.score, score.success_rate, score.decided_samples) == (0.0, 0.0, 0)
    assert score.total_samples == 7


def test_abandoned_runs_do_not_count_against_success_rate() -> None:
    """放弃（取消 / 人拒绝）不是 skill 的失败，不进成功率分母。"""
    with_abandoned = WilsonFitnessScorer().score(_fitness("s", successes=8, abandoned=20))
    without = WilsonFitnessScorer().score(_fitness("s", successes=8))
    assert with_abandoned.score == without.score
    assert with_abandoned.success_rate == 1.0


def test_more_evidence_scores_higher_at_same_rate() -> None:
    """同样的成功率，样本越多分越高——一次成功不足以被信任。"""
    scorer = WilsonFitnessScorer()
    once = scorer.score(_fitness("s", successes=1))
    many = scorer.score(_fitness("s", successes=50))
    assert once.success_rate == many.success_rate == 1.0
    assert once.score < many.score < 1.0


def test_lower_bound_matches_wilson_formula() -> None:
    score = WilsonFitnessScorer(z=1.96).score(_fitness("s", successes=8, failures=2))
    n, p, z = 10, 0.8, 1.96
    expected = (
        p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    ) / (1 + z * z / n)
    assert score.success_lower_bound == pytest.approx(expected)
    assert score.score == pytest.approx(expected)


def test_cost_lowers_score_only_when_scale_configured() -> None:
    fitness = _fitness("s", successes=10, tokens=20_000)
    free = WilsonFitnessScorer().score(fitness)
    priced = WilsonFitnessScorer(cost_scale_tokens=2_000).score(fitness)
    assert free.mean_cost_tokens == priced.mean_cost_tokens == 2_000.0
    assert priced.score == pytest.approx(free.score / 2)


def test_score_stays_in_unit_interval() -> None:
    for successes, failures in [(0, 1), (0, 1000), (1, 0), (1000, 0), (3, 7)]:
        score = WilsonFitnessScorer().score(_fitness("s", successes, failures)).score
        assert 0.0 <= score <= 1.0


@pytest.mark.parametrize("kwargs", [{"z": 0}, {"z": -1}, {"cost_scale_tokens": 0}])
def test_scorer_rejects_invalid_parameters(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        WilsonFitnessScorer(**kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# 工作集规划
# ------------------------------------------------------------------


def test_promotes_top_scores_within_budget() -> None:
    policy = WorkingSetPolicy(budget=2, promote_min_score=0.5, promote_min_samples=5)
    plan = plan_working_set(
        [_score("a", 0.9), _score("b", 0.7), _score("c", 0.8), _score("d", 0.4)],
        promoted=frozenset(),
        policy=policy,
    )
    assert plan.promoted == ("a", "c")
    assert plan.promote == ("a", "c")
    assert plan.evict == ()


def test_too_few_samples_is_not_promoted() -> None:
    policy = WorkingSetPolicy(budget=5, promote_min_score=0.1, promote_min_samples=5)
    plan = plan_working_set(
        [_score("a", 0.9, samples=4)], promoted=frozenset(), policy=policy
    )
    assert plan.promoted == ()


def test_over_budget_evicts_lowest_score_not_oldest() -> None:
    """超预算逐出最低分者（按战绩，不按先后）。"""
    policy = WorkingSetPolicy(budget=2, promote_min_score=0.5, promote_min_samples=5)
    plan = plan_working_set(
        [_score("old", 0.55), _score("mid", 0.7), _score("new", 0.95)],
        promoted=frozenset({"old", "mid"}),
        policy=policy,
    )
    assert plan.promoted == ("new", "mid")
    assert plan.promote == ("new",)
    assert plan.evict == ("old",)


def test_promoted_skill_falling_below_threshold_is_evicted() -> None:
    policy = WorkingSetPolicy(budget=3, promote_min_score=0.5, promote_min_samples=5)
    plan = plan_working_set(
        [_score("a", 0.3)], promoted=frozenset({"a"}), policy=policy
    )
    assert plan.promoted == ()
    assert plan.evict == ("a",)


def test_promoted_skill_without_any_record_is_evicted() -> None:
    """工作集里的 skill 若已无战绩记录（被重置 / 已删除），不保留。"""
    plan = plan_working_set(
        [], promoted=frozenset({"ghost"}), policy=WorkingSetPolicy(budget=3)
    )
    assert plan.evict == ("ghost",)


def test_quarantines_frequently_selected_but_failing_skill() -> None:
    """高选中、低成功：描述过度承诺，隔离且不得提拔。"""
    policy = WorkingSetPolicy(
        budget=3, quarantine_min_samples=5, quarantine_max_success_rate=0.2,
    )
    plan = plan_working_set(
        [_score("fake", 0.02, samples=20, rate=0.1), _score("ok", 0.8)],
        promoted=frozenset({"fake"}),
        policy=policy,
    )
    assert plan.quarantined == ("fake",)
    assert plan.quarantine == ("fake",)
    assert plan.evict == ("fake",)
    assert plan.promoted == ("ok",)


def test_few_failures_do_not_quarantine() -> None:
    policy = WorkingSetPolicy(budget=3, quarantine_min_samples=5)
    plan = plan_working_set(
        [_score("new", 0.0, samples=2, rate=0.0)], promoted=frozenset(), policy=policy
    )
    assert plan.quarantined == ()


def test_release_when_record_no_longer_qualifies() -> None:
    """战绩被重置或好转后解除隔离。"""
    policy = WorkingSetPolicy(budget=3)
    plan = plan_working_set(
        [_score("fixed", 0.7, samples=10, rate=0.9)],
        promoted=frozenset(),
        quarantined=frozenset({"fixed", "gone"}),
        policy=policy,
    )
    assert plan.quarantined == ()
    assert plan.release == ("fixed", "gone")
    assert plan.promoted == ("fixed",)


def test_zero_budget_promotes_nothing_but_still_quarantines() -> None:
    plan = plan_working_set(
        [_score("a", 0.9), _score("fake", 0.0, samples=9, rate=0.0)],
        promoted=frozenset(),
        policy=WorkingSetPolicy(budget=0),
    )
    assert plan.promoted == ()
    assert plan.quarantined == ("fake",)


def test_ties_break_by_samples_then_id() -> None:
    policy = WorkingSetPolicy(budget=2, promote_min_score=0.5, promote_min_samples=5)
    plan = plan_working_set(
        [_score("b", 0.7, samples=10), _score("a", 0.7, samples=10),
         _score("c", 0.7, samples=30)],
        promoted=frozenset(),
        policy=policy,
    )
    assert plan.promoted == ("c", "a")


def test_duplicate_skill_scores_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        plan_working_set(
            [_score("a", 0.9), _score("a", 0.8)],
            promoted=frozenset(),
            policy=WorkingSetPolicy(budget=1),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"budget": -1},
        {"budget": 1, "promote_min_score": 1.5},
        {"budget": 1, "promote_min_samples": 0},
        {"budget": 1, "quarantine_min_samples": 0},
        {"budget": 1, "quarantine_max_success_rate": -0.1},
    ],
)
def test_policy_rejects_invalid_parameters(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        WorkingSetPolicy(**kwargs)  # type: ignore[arg-type]


def test_plan_has_no_changes_flag() -> None:
    policy = WorkingSetPolicy(budget=1, promote_min_score=0.5, promote_min_samples=5)
    stable = plan_working_set([_score("a", 0.9)], promoted=frozenset({"a"}), policy=policy)
    assert not stable.changed
    moved = plan_working_set([_score("a", 0.9)], promoted=frozenset(), policy=policy)
    assert moved.changed
