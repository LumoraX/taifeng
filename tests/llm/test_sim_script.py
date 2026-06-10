"""sim/script.py 单测 —— SimTurn 默认值 / MockTurn 字段兼容 / SimFault 变体 / SimExpect 边界。"""

from __future__ import annotations

import pytest

from taifeng.llm.providers.sim import SimExpect, SimFault, SimScriptExhausted, SimTurn
from taifeng.llm.types import TokenUsage


def test_sim_turn_defaults():
    """SimTurn 零参构造的全部默认值（与旧 MockTurn 语义对齐 + 新字段全 None/空）。"""
    turn = SimTurn()
    assert turn.text == ""
    assert turn.tool_calls == []
    assert turn.usage.input_tokens == 100
    assert turn.usage.output_tokens == 50
    assert turn.delay_seconds == 0.0
    assert turn.structured is None
    assert turn.cache_read is None
    assert turn.cache_creation == 0
    assert turn.request_id is None
    # conformance 新增字段默认不生效
    assert turn.finish is None
    assert turn.expect is None
    assert turn.fault is None
    assert turn.await_signal is None
    assert turn.emit_signal is None


def test_sim_turn_accepts_all_mock_turn_kwargs():
    """字段兼容红线：SimTurn 必须接受旧 MockTurn 的全部构造参数（机械迁移保证）。"""
    turn = SimTurn(
        text="先调用工具",
        tool_calls=[{"id": "c1", "name": "read_skill", "arguments": '{"skill_id": "x"}'}],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        delay_seconds=0.5,
        structured={"k": "v"},
        cache_read=42,
        cache_creation=7,
        request_id="req-1",
    )
    assert turn.tool_calls[0]["name"] == "read_skill"
    assert turn.cache_read == 42
    assert turn.request_id == "req-1"


def test_sim_turn_super_long_text():
    """超长 text 边界：构造不截断、不报错（流式切片在 client 层处理）。"""
    long_text = "甲" * 100_000
    assert len(SimTurn(text=long_text).text) == 100_000


def test_sim_fault_factories():
    """SimFault 四个工厂各产出对应互斥变体。"""
    rl = SimFault.rate_limit(retry_after_seconds=2.5)
    assert rl.kind == "rate_limit"
    assert rl.retry_after_seconds == 2.5

    se = SimFault.server_error()
    assert se.kind == "server_error"
    assert se.retry_after_seconds is None

    ma = SimFault.malformed_arguments()
    assert ma.kind == "malformed_arguments"

    ts = SimFault.truncate_stream(after_events=3)
    assert ts.kind == "truncate_stream"
    assert ts.after_events == 3


def test_sim_fault_truncate_negative_rejected():
    """truncate_stream 负数事件计数直接拒绝（不静默修正）。"""
    with pytest.raises(ValueError):
        SimFault.truncate_stream(after_events=-1)


def test_sim_fault_frozen():
    """SimFault 不可变：变体构造后禁止篡改。"""
    fault = SimFault.server_error()
    with pytest.raises(AttributeError):
        fault.kind = "rate_limit"  # type: ignore[misc]


def test_sim_expect_defaults_and_bounds():
    """SimExpect 默认全空；上下界可单独/同时设置。"""
    empty = SimExpect()
    assert empty.must_contain == ()
    assert empty.must_include_output_for == ()
    assert empty.min_messages is None
    assert empty.predicate is None

    bounded = SimExpect(must_contain=("工具结果",), min_messages=2, max_messages=10)
    assert bounded.must_contain == ("工具结果",)
    assert bounded.min_messages == 2
    assert bounded.max_messages == 10


def test_sim_script_exhausted_is_plain_exception():
    """SimScriptExhausted 是普通 Exception，不得落入 LLMError 体系（否则会被 retry 消化）。"""
    from taifeng.llm.errors import LLMError

    assert issubclass(SimScriptExhausted, Exception)
    assert not issubclass(SimScriptExhausted, LLMError)
