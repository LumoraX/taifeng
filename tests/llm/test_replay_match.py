"""replay_match —— 采样 id 一致重命名下的两段式请求匹配（ADR 0070）。"""

from __future__ import annotations

from typing import Any

import pytest

from taifeng.llm.audit_redaction import canonical_attempt_digest
from taifeng.llm.providers.replay_match import (
    ReplayRequestShapeError,
    locator_digest,
    matches_recorded_digest,
    run_derived_sample_ids,
)
from taifeng.loop.turn_helpers import _responses_sample_id


def _sample(thread: str, submission: str, turn: int, iteration: int) -> str:
    """用内核真实派生规则构造采样 id。"""
    return _responses_sample_id(
        thread_id=thread, submission_id=submission, turn_index=turn, iteration=iteration
    )


def _request(first: str, second: str, *, ciphertext: str = "c1") -> dict[str, Any]:
    """两个采样的最小请求 JSON：reasoning state + function call + output + 新一轮消息。"""
    return {
        "model": "m",
        "input_items": [
            {"type": "message", "role": "user", "content": "hi", "sample_id": None},
            {"type": "provider_state", "sample_id": first, "output_index": 0,
             "state": {"provider": "openai", "protocol": "responses", "item_type": "reasoning",
                       "payload": {"id": "rs", "type": "reasoning",
                                   "encrypted_content": ciphertext}}},
            {"type": "function_call", "call_id": "c", "name": "t", "arguments": "{}",
             "sample_id": first, "output_index": 1},
            {"type": "function_call_output", "call_id": "c", "output": "ok",
             "origin_sample_id": first},
            {"type": "message", "role": "assistant", "content": "done", "sample_id": second,
             "output_index": 0},
            {"type": "message", "role": "user", "content": "again", "sample_id": None},
        ],
    }


def test_kernel_sample_id_derivation_matches_grammar() -> None:
    """守护：内核派生的采样 id 必须被识别为运行派生（两处规则漂移即红）。"""
    sample = _sample("thr_a1", "sub_b2", 3, 1)

    assert run_derived_sample_ids({"input_items": [{"sample_id": sample}]}) == (sample,)
    assert run_derived_sample_ids(
        {"input_items": [{"sample_id": "legacy:sample:4"}]}
    ) == ()


def test_locator_digest_equal_under_consistent_renaming() -> None:
    """不同运行的 thread / submission / turn 编号不同，但一致重命名后定位摘要相等。"""
    recorded = _request(_sample("thr_r", "sub_1", 0, 0), _sample("thr_r", "sub_1", 0, 1))
    replayed = _request(_sample("thr_x", "sub_9", 2, 0), _sample("thr_x", "sub_9", 2, 1))

    assert locator_digest("p", "m", recorded)[0] == locator_digest("p", "m", replayed)[0]


def test_locator_digest_distinguishes_sample_grouping() -> None:
    """归组不同（两项同属一个采样 vs 分属两个）不是重命名能抹平的差异。"""
    one = _sample("thr_r", "sub_1", 0, 0)
    grouped = _request(one, one)
    split = _request(one, _sample("thr_r", "sub_1", 0, 1))

    assert locator_digest("p", "m", grouped)[0] != locator_digest("p", "m", split)[0]


def test_full_digest_recheck_maps_back_to_recorded_ids() -> None:
    """复核：完整请求改写回录制采样 id 后摘要与录制 canonical 摘要逐字节相等。"""
    recorded_ids = (_sample("thr_r", "sub_1", 0, 0), _sample("thr_r", "sub_1", 0, 1))
    recorded = _request(*recorded_ids)
    digest = canonical_attempt_digest("p", "m", recorded)
    replayed = _request(_sample("thr_x", "sub_9", 5, 0), _sample("thr_x", "sub_9", 5, 1))

    assert matches_recorded_digest("p", "m", replayed, recorded_ids, digest)


def test_full_digest_recheck_rejects_different_ciphertext() -> None:
    """密文在安全投影里被脱敏，只能靠复核发现不同——不因脱敏放宽匹配。"""
    recorded_ids = (_sample("thr_r", "sub_1", 0, 0), _sample("thr_r", "sub_1", 0, 1))
    digest = canonical_attempt_digest("p", "m", _request(*recorded_ids))
    replayed = _request(
        _sample("thr_x", "sub_9", 0, 0), _sample("thr_x", "sub_9", 0, 1), ciphertext="forged"
    )

    assert not matches_recorded_digest("p", "m", replayed, recorded_ids, digest)
    assert not matches_recorded_digest("p", "m", replayed, recorded_ids[:1], digest)


def test_request_without_input_items_is_rejected() -> None:
    """缺 input_items 列表的请求 JSON 显式报错，不当作空请求。"""
    with pytest.raises(ReplayRequestShapeError):
        locator_digest("p", "m", {"model": "m"})
