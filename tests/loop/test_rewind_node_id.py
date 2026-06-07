"""turn 限定 node_id + count_turns 结构化 turn 序号。"""
from __future__ import annotations

from taifeng.conversation.models import assistant_message, user_message
from taifeng.loop.rewind import RewindLog, count_turns


def _hist(*kinds: str) -> list:
    """造一串只关心 kind 的 ResponseItem。"""
    out = []
    for k in kinds:
        if k == "user":
            out.append(user_message("u", thread_id="t"))
        else:
            out.append(assistant_message("a", thread_id="t", model="m"))
    return out


def test_count_turns_counts_user_messages():
    """count_turns = 累积 user_message 数(1-based 的最大 k)。"""
    assert count_turns([]) == 0
    assert count_turns(_hist("user", "assistant", "user")) == 2


def test_record_iteration_node_id_is_turn_qualified():
    log = RewindLog()
    cp = log.record_iteration(turn_index=2, iteration_index=3, history_len=5, cache_anchor=-1)
    assert cp.node_id == "t2:it3"
    assert cp.turn_index == 2


def test_record_dispatch_node_id_is_turn_qualified():
    log = RewindLog()
    cp = log.record_dispatch(
        turn_index=1, iteration_index=1, iteration_history_len=4, cache_anchor=-1,
        call_id="c1", target_id="read_skill", inner_history_len=6, args_digest="{}",
    )
    assert cp.node_id == "t1:disp0"
    assert cp.turn_index == 1
