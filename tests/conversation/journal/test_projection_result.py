"""ProjectionResult 自洽状态不变量测试。"""

from __future__ import annotations

from typing import Any

import pytest

from taifeng.conversation.journal.projector import ProjectionResult


@pytest.mark.parametrize(
    "values",
    [
        {"thread_id": "", "projected_seq": 0, "stale": False},
        {"thread_id": "thr_1", "projected_seq": -1, "stale": False},
        {
            "thread_id": "thr_1",
            "projected_seq": 1,
            "stale": False,
            "failure_class": "OSError",
        },
        {
            "thread_id": "thr_1",
            "projected_seq": 1,
            "stale": False,
            "failure_record_id": "rec_1",
        },
        {"thread_id": "thr_1", "projected_seq": 1, "stale": True},
        {
            "thread_id": "thr_1",
            "projected_seq": 1,
            "stale": True,
            "failure_class": "",
        },
        {
            "thread_id": "thr_1",
            "projected_seq": 1,
            "stale": True,
            "failure_class": "OSError",
            "failure_record_id": "",
        },
    ],
)
def test_projection_result_rejects_invalid_state_matrix(values: dict[str, Any]) -> None:
    """非法水位/health/failure 组合必须在 DTO 构造时直接拒绝。"""
    with pytest.raises(ValueError):
        ProjectionResult(**values)


def test_projection_result_accepts_complete_healthy_and_stale_states() -> None:
    """healthy 不携 failure；stale 必须携稳定分类且 record id 可选。"""
    healthy = ProjectionResult(thread_id="thr_1", projected_seq=0, stale=False)
    stale = ProjectionResult(
        thread_id="thr_1",
        projected_seq=4,
        stale=True,
        failure_class="OSError",
        failure_record_id="rec_5",
    )

    assert healthy.failure_class is None and healthy.failure_record_id is None
    assert stale.failure_class == "OSError"
    assert stale.failure_record_id == "rec_5"
