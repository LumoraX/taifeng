"""生效的工作集：记战绩、重算、结论快照（相位 5 生效，ADR 0090）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng.skill import SkillDefinition
from taifeng.skill.fitness import InMemorySkillFitnessStore
from taifeng.skill.outcome import SkillExecutionRecord
from taifeng.skill.registry import SkillSnapshot
from taifeng.skill.selection import SelectionCandidate, ThresholdSelectionPolicy
from taifeng.skill.trust import SourceTrustPolicy
from taifeng.skill.working_set import (
    SkillFitnessScore,
    TierRule,
    WorkingSetPolicy,
    plan_working_set,
)
from taifeng.skill.working_set_runtime import (
    EMPTY_VIEW,
    WORKING_SET_VIEW_EXTRAS_KEY,
    SkillWorkingSet,
    WorkingSetView,
    view_from_extras,
)


def _record(
    skill_id: str, call_id: str, outcome: str, *, tier: str | None = None,
) -> SkillExecutionRecord:
    return SkillExecutionRecord(
        skill_id=skill_id, call_id=call_id, parent_call_id=None, depth=1, source="user",
        trust_tier=tier, selection_origin="whitelist", selection_confidence=None,
        outcome=outcome, outcome_signal_source="structural",  # type: ignore[arg-type]
        end_reason="completed", error_detail=None, cost_tokens=10, cost_duration_ms=5,
        cost_iterations=1, ts_unix=100)


def _score(skill_id: str, successes: int, failures: int) -> SkillFitnessScore:
    decided = successes + failures
    rate = successes / decided if decided else 0.0
    return SkillFitnessScore(
        skill_id=skill_id, score=rate, success_rate=rate, success_lower_bound=rate,
        decided_samples=decided, total_samples=decided, mean_cost_tokens=0.0,
    )


def _policy(**kwargs: object) -> WorkingSetPolicy:
    defaults: dict[str, object] = {
        "budget": 2, "promote_min_score": 0.2, "promote_min_samples": 1,
        "quarantine_min_samples": 2, "quarantine_max_success_rate": 0.0,
    }
    return WorkingSetPolicy(**{**defaults, **kwargs})  # type: ignore[arg-type]


def _snapshot(*pairs: tuple[str, str]) -> SkillSnapshot:
    skills = tuple(
        SkillDefinition(
            id=skill_id, name=skill_id, description="d", version="1", body="",
            body_path=Path("/nonexistent") / skill_id / "SKILL.md", type="atomic",
            source=source,  # type: ignore[arg-type]
        )
        for skill_id, source in pairs
    )
    return SkillSnapshot(version=1, skills=skills, reachable_graph={})


# ------------------------------------------------------------------
# 按来源信任层级调整门槛（纯规划）
# ------------------------------------------------------------------


def test_tier_rule_raises_the_promotion_bar() -> None:
    policy = _policy(tier_rules={"untrusted": TierRule(promote_min_samples=5)})
    scores = [_score("own", 2, 0), _score("bought", 2, 0)]

    plan = plan_working_set(
        scores, promoted=frozenset(), policy=policy,
        tiers={"own": "standard", "bought": "untrusted"},
    )

    assert plan.promoted == ("own",)


def test_tier_rule_can_forbid_promotion() -> None:
    policy = _policy(tier_rules={"untrusted": TierRule(promotable=False)})

    plan = plan_working_set(
        [_score("bought", 50, 0)], promoted=frozenset({"bought"}), policy=policy,
        tiers={"bought": "untrusted"},
    )

    assert plan.promoted == ()
    assert plan.evict == ("bought",)


def test_tier_rule_can_exempt_from_quarantine() -> None:
    policy = _policy(tier_rules={"trusted": TierRule(quarantine_exempt=True)})
    scores = [_score("core", 0, 9), _score("other", 0, 9)]

    plan = plan_working_set(
        scores, promoted=frozenset(), policy=policy,
        tiers={"core": "trusted", "other": "standard"},
    )

    assert plan.quarantined == ("other",)


def test_tier_rule_quarantines_sooner() -> None:
    policy = _policy(
        quarantine_min_samples=5,
        tier_rules={"untrusted": TierRule(quarantine_min_samples=2)},
    )
    scores = [_score("own", 0, 2), _score("bought", 0, 2)]

    plan = plan_working_set(
        scores, promoted=frozenset(), policy=policy,
        tiers={"own": "standard", "bought": "untrusted"},
    )

    assert plan.quarantined == ("bought",)


def test_unknown_tier_uses_the_general_policy() -> None:
    policy = _policy(tier_rules={"untrusted": TierRule(promotable=False)})

    without_tiers = plan_working_set([_score("a", 3, 0)], promoted=frozenset(), policy=policy)
    unlisted = plan_working_set(
        [_score("a", 3, 0)], promoted=frozenset(), policy=policy, tiers={"b": "untrusted"},
    )

    assert without_tiers.promoted == unlisted.promoted == ("a",)


def test_tier_does_not_change_the_score_order() -> None:
    """层级只调门槛：过了门槛的 skill 仍按战绩分排，不因层级高而插队。"""
    policy = _policy(budget=1)
    scores = [_score("core", 3, 2), _score("bought", 5, 0)]

    plan = plan_working_set(
        scores, promoted=frozenset(), policy=policy,
        tiers={"core": "trusted", "bought": "untrusted"},
    )

    assert plan.promoted == ("bought",)


@pytest.mark.parametrize("field", ["promote_min_samples", "quarantine_min_samples"])
def test_tier_rule_validation(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        TierRule(**{field: 0})  # type: ignore[arg-type]


# ------------------------------------------------------------------
# SkillWorkingSet
# ------------------------------------------------------------------


async def test_observe_promotes_and_reports_the_change() -> None:
    working_set = SkillWorkingSet(store=InMemorySkillFitnessStore(), policy=_policy())

    changes = await working_set.observe(_record("alpha", "c1", "success", tier="standard"))

    assert [(c.kind, c.skill_id) for c in changes] == [("promoted", "alpha")]
    assert changes[0].trigger_call_id == "c1"
    assert changes[0].trust_tier == "standard"
    payload = changes[0].as_payload()
    assert payload["skill_id"] == "alpha"
    assert payload["decided_samples"] == 1
    assert payload["success_rate"] == 1.0
    assert working_set.promoted == ("alpha",)
    assert working_set.view().promoted == ("alpha",)


async def test_duplicate_delivery_is_not_replanned() -> None:
    working_set = SkillWorkingSet(store=InMemorySkillFitnessStore(), policy=_policy())
    record = _record("alpha", "c1", "success")

    await working_set.observe(record)

    assert await working_set.observe(record) == ()


async def test_over_budget_evicts_the_lowest_score() -> None:
    working_set = SkillWorkingSet(
        store=InMemorySkillFitnessStore(), policy=_policy(budget=1)
    )
    await working_set.observe(_record("alpha", "a1", "success"))

    await working_set.observe(_record("beta", "b1", "success"))
    changes = await working_set.observe(_record("beta", "b2", "success"))

    assert [(c.kind, c.skill_id) for c in changes] == [
        ("evicted", "alpha"), ("promoted", "beta"),
    ]
    assert working_set.promoted == ("beta",)


@pytest.mark.parametrize(
    ("effect", "hidden", "blocked"),
    [
        ("flag", frozenset(), frozenset()),
        ("hide", frozenset({"bad"}), frozenset()),
        ("block", frozenset({"bad"}), frozenset({"bad"})),
    ],
)
async def test_quarantine_effect(
    effect: str, hidden: frozenset[str], blocked: frozenset[str],
) -> None:
    working_set = SkillWorkingSet(
        store=InMemorySkillFitnessStore(), policy=_policy(),
        quarantine_effect=effect,  # type: ignore[arg-type]
    )
    await working_set.observe(_record("bad", "c1", "failure"))

    changes = await working_set.observe(_record("bad", "c2", "failure"))

    assert [(c.kind, c.skill_id) for c in changes] == [("quarantined", "bad")]
    assert working_set.quarantined == frozenset({"bad"})
    view = working_set.view()
    assert (view.hidden, view.blocked) == (hidden, blocked)
    assert working_set.blocks("bad") is (effect == "block")
    assert working_set.blocks("other") is False


async def test_recovery_releases_the_quarantine() -> None:
    working_set = SkillWorkingSet(store=InMemorySkillFitnessStore(), policy=_policy())
    await working_set.observe(_record("bad", "c1", "failure"))
    await working_set.observe(_record("bad", "c2", "failure"))

    changes = await working_set.observe(_record("bad", "c3", "success"))

    assert ("released", "bad") in [(c.kind, c.skill_id) for c in changes]
    assert working_set.quarantined == frozenset()


async def test_abandoned_runs_do_not_count_towards_quarantine() -> None:
    working_set = SkillWorkingSet(store=InMemorySkillFitnessStore(), policy=_policy())

    await working_set.observe(_record("alpha", "c1", "abandoned"))
    changes = await working_set.observe(_record("alpha", "c2", "abandoned"))

    assert changes == ()
    assert working_set.quarantined == frozenset()


async def test_restore_recomputes_from_stored_outcomes() -> None:
    store = InMemorySkillFitnessStore()
    await store.record(_record("alpha", "a1", "success"))
    await store.record(_record("bad", "b1", "failure"))
    await store.record(_record("bad", "b2", "failure"))
    working_set = SkillWorkingSet(store=store, policy=_policy())

    changes = await working_set.restore(_snapshot(("alpha", "user"), ("bad", "user")), None)

    assert [(c.kind, c.skill_id) for c in changes] == [
        ("quarantined", "bad"), ("promoted", "alpha"),
    ]
    assert all(c.trigger_call_id is None for c in changes)
    assert all(c.trust_tier is None for c in changes)
    # 只重算一次
    assert await working_set.restore(_snapshot(), None) == ()


async def test_restore_applies_trust_tiers_from_the_registry() -> None:
    store = InMemorySkillFitnessStore()
    await store.record(_record("own", "a1", "success"))
    await store.record(_record("bought", "b1", "success"))
    working_set = SkillWorkingSet(
        store=store,
        policy=_policy(tier_rules={"untrusted": TierRule(promotable=False)}),
    )

    changes = await working_set.restore(
        _snapshot(("own", "user"), ("bought", "marketplace")), SourceTrustPolicy()
    )

    assert [(c.kind, c.skill_id, c.trust_tier) for c in changes] == [
        ("promoted", "own", "standard"),
    ]


async def test_observed_tier_is_used_for_planning() -> None:
    working_set = SkillWorkingSet(
        store=InMemorySkillFitnessStore(),
        policy=_policy(tier_rules={"untrusted": TierRule(promotable=False)}),
    )

    changes = await working_set.observe(_record("bought", "c1", "success", tier="untrusted"))

    assert changes == ()
    assert working_set.promoted == ()


def test_construction_is_validated() -> None:
    class _WriteOnly:
        async def record(self, record: SkillExecutionRecord) -> None: ...

        async def fitness(self, skill_id: str) -> None: ...

    with pytest.raises(TypeError, match="all_fitness"):
        SkillWorkingSet(store=_WriteOnly(), policy=_policy())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="quarantine_effect"):
        SkillWorkingSet(
            store=InMemorySkillFitnessStore(), policy=_policy(),
            quarantine_effect="erase",  # type: ignore[arg-type]
        )


def test_view_from_extras() -> None:
    view = WorkingSetView(promoted=("a",))

    assert view_from_extras({WORKING_SET_VIEW_EXTRAS_KEY: view}) is view
    assert view_from_extras({}) is EMPTY_VIEW
    assert view_from_extras({WORKING_SET_VIEW_EXTRAS_KEY: "junk"}) is EMPTY_VIEW


# ------------------------------------------------------------------
# 来源信任层级参与选择分流
# ------------------------------------------------------------------


def test_untrusted_candidate_must_be_tried_first() -> None:
    policy = ThresholdSelectionPolicy(trial_tiers=frozenset({"untrusted"}))

    routed = policy.route([
        SelectionCandidate("own", 0.95, trust_tier="standard"),
        SelectionCandidate("bought", 0.85, trust_tier="untrusted"),
        SelectionCandidate("weak", 0.1, trust_tier="untrusted"),
    ])

    assert [(r.skill_id, r.route) for r in routed] == [
        ("own", "proceed"), ("bought", "trial"), ("weak", "escalate"),
    ]
    assert routed[1].reason == "source trust tier is untrusted"


def test_trust_tier_is_ignored_by_default() -> None:
    routed = ThresholdSelectionPolicy().route([
        SelectionCandidate("bought", 0.9, trust_tier="untrusted"),
    ])

    assert routed[0].route == "proceed"
