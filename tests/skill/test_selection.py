"""按选择置信度分流的纯函数与数据契约（ADR 0088）。"""

from __future__ import annotations

import json

import pytest

from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)
from taifeng.skill.selection import (
    SelectionCandidate,
    SelectionConfidencePolicy,
    ThresholdSelectionPolicy,
    latest_selection,
    was_read_after,
)

T = "thr"


def _routes(policy: ThresholdSelectionPolicy, **confidences: float) -> dict[str, str]:
    candidates = [SelectionCandidate(k, v) for k, v in confidences.items()]
    return {r.skill_id: r.route for r in policy.route(candidates)}


def test_default_policy_is_a_selection_policy() -> None:
    assert isinstance(ThresholdSelectionPolicy(), SelectionConfidencePolicy)


def test_three_bands() -> None:
    policy = ThresholdSelectionPolicy(tau_high=0.75, tau_low=0.4, ambiguity_margin=0.05)
    assert _routes(policy, high=0.9, mid=0.6, low=0.2) == {
        "high": "proceed", "mid": "trial", "low": "escalate",
    }


def test_band_boundaries_are_inclusive_at_the_lower_edge() -> None:
    policy = ThresholdSelectionPolicy(tau_high=0.75, tau_low=0.4, ambiguity_margin=0)
    assert _routes(policy, a=0.75, b=0.4, c=0.3999) == {
        "a": "proceed", "b": "trial", "c": "escalate",
    }


def test_close_top_candidates_must_both_be_tried() -> None:
    policy = ThresholdSelectionPolicy(tau_high=0.75, tau_low=0.4, ambiguity_margin=0.05)
    assert _routes(policy, finance=0.91, legal=0.88, other=0.8) == {
        "finance": "trial", "legal": "trial", "other": "proceed",
    }


def test_clear_winner_is_not_contested() -> None:
    policy = ThresholdSelectionPolicy(tau_high=0.75, tau_low=0.4, ambiguity_margin=0.05)
    assert _routes(policy, finance=0.95, legal=0.8) == {
        "finance": "proceed", "legal": "proceed",
    }


def test_candidates_below_tau_high_do_not_contest_the_winner() -> None:
    """并列判定只在可直接派发的候选之间做：本就要试用的候选不连累赢家。"""
    policy = ThresholdSelectionPolicy(tau_high=0.75, tau_low=0.4, ambiguity_margin=0.05)
    assert _routes(policy, winner=0.76, runner=0.74) == {
        "winner": "proceed", "runner": "trial",
    }


def test_order_and_reasons_are_preserved() -> None:
    policy = ThresholdSelectionPolicy()
    routed = policy.route([SelectionCandidate("b", 0.1), SelectionCandidate("a", 0.9)])
    assert [r.skill_id for r in routed] == ["b", "a"]
    assert "0.10" in routed[0].reason and "0.40" in routed[0].reason
    assert "0.90" in routed[1].reason


def test_empty_candidates() -> None:
    assert ThresholdSelectionPolicy().route([]) == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tau_high": 1.2},
        {"tau_low": -0.1},
        {"tau_high": 0.3, "tau_low": 0.5},
        {"ambiguity_margin": -0.01},
    ],
)
def test_invalid_thresholds_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ThresholdSelectionPolicy(**kwargs)


# ------------------------------------------------------------------
# 由 history 推导
# ------------------------------------------------------------------


def _search(call_id: str, payload: object) -> list[ResponseItem]:
    return [
        function_call(call_id, "search_skills", '{"query": "q"}', thread_id=T),
        function_call_output(
            call_id=call_id, output=json.dumps(payload, ensure_ascii=False), thread_id=T
        ),
    ]


def _read(call_id: str, skill_id: str, *, is_error: bool = False) -> list[ResponseItem]:
    return [
        function_call(call_id, "read_skill", json.dumps({"skill_id": skill_id}), thread_id=T),
        function_call_output(call_id=call_id, output="说明书", thread_id=T, is_error=is_error),
    ]


def _candidate(skill_id: str, confidence: float, route: str) -> dict[str, object]:
    return {"skill_id": skill_id, "description": "d", "confidence": confidence, "route": route}


def test_latest_selection_reads_the_route_the_model_saw() -> None:
    history = [
        user_message("任务", thread_id=T),
        *_search("s1", [_candidate("x", 0.6, "trial"), _candidate("y", 0.9, "proceed")]),
    ]

    selection = latest_selection(history, "x")

    assert selection is not None
    assert (selection.route, selection.confidence, selection.search_index) == ("trial", 0.6, 2)
    assert latest_selection(history, "unknown") is None


def test_latest_search_wins() -> None:
    history = [
        user_message("任务", thread_id=T),
        *_search("s1", [_candidate("x", 0.3, "escalate")]),
        *_search("s2", [_candidate("x", 0.9, "proceed")]),
    ]
    selection = latest_selection(history, "x")
    assert selection is not None and selection.route == "proceed"


def test_low_confidence_no_match_shape_is_understood() -> None:
    history = [
        user_message("任务", thread_id=T),
        *_search("s1", {
            "no_match": True, "hint": "h",
            "low_confidence": [_candidate("x", 0.1, "escalate")],
        }),
    ]
    selection = latest_selection(history, "x")
    assert selection is not None and selection.route == "escalate"


def test_recall_from_an_earlier_turn_does_not_count() -> None:
    history = [
        user_message("上一轮", thread_id=T),
        *_search("s1", [_candidate("x", 0.2, "escalate")]),
        assistant_message("答", thread_id=T, model="m"),
        user_message("这一轮", thread_id=T),
    ]
    assert latest_selection(history, "x") is None


def test_results_without_routes_are_ignored() -> None:
    """未启用分流时的召回结果不带 route：不构成约束。"""
    history = [
        user_message("任务", thread_id=T),
        *_search("s1", [{"skill_id": "x", "description": "d", "confidence": 0.1}]),
    ]
    assert latest_selection(history, "x") is None


@pytest.mark.parametrize(
    "output", ["not json", '"text"', "[1, 2]", '[{"skill_id": 1, "route": "trial"}]',
               '[{"skill_id": "x", "route": "maybe"}]'],
)
def test_malformed_search_output_yields_no_selection(output: str) -> None:
    history = [
        user_message("任务", thread_id=T),
        function_call("s1", "search_skills", "{}", thread_id=T),
        function_call_output(call_id="s1", output=output, thread_id=T),
    ]
    assert latest_selection(history, "x") is None


def test_failed_search_is_ignored() -> None:
    history = [
        user_message("任务", thread_id=T),
        function_call("s1", "search_skills", "{}", thread_id=T),
        function_call_output(
            call_id="s1", output=json.dumps([_candidate("x", 0.9, "proceed")]),
            thread_id=T, is_error=True,
        ),
    ]
    assert latest_selection(history, "x") is None


def test_other_tools_with_similar_output_are_ignored() -> None:
    history = [
        user_message("任务", thread_id=T),
        function_call("o1", "other_tool", "{}", thread_id=T),
        function_call_output(
            call_id="o1", output=json.dumps([_candidate("x", 0.1, "escalate")]), thread_id=T
        ),
    ]
    assert latest_selection(history, "x") is None


def test_reading_the_manual_after_the_search_counts_as_a_trial() -> None:
    history = [
        user_message("任务", thread_id=T),
        *_search("s1", [_candidate("x", 0.6, "trial")]),
        *_read("r1", "x"),
    ]
    assert was_read_after(history, "x", after_index=2)
    assert not was_read_after(history, "y", after_index=2)


def test_reading_before_the_search_does_not_count() -> None:
    history = [
        user_message("任务", thread_id=T),
        *_read("r1", "x"),
        *_search("s1", [_candidate("x", 0.6, "trial")]),
    ]
    assert not was_read_after(history, "x", after_index=4)


def test_failed_read_does_not_count() -> None:
    history = [
        user_message("任务", thread_id=T),
        *_search("s1", [_candidate("x", 0.6, "trial")]),
        *_read("r1", "x", is_error=True),
    ]
    assert not was_read_after(history, "x", after_index=2)
