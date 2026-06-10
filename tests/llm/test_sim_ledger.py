"""sim/server.py RequestLedger 单测 —— 请求侦察断言面。"""

from __future__ import annotations

import pytest

from taifeng.llm.providers.sim.server import RequestLedger
from taifeng.llm.types import ApiMessage, ApiRequest, ToolSpecRef


def _req(messages: list[ApiMessage], *, system: list[str] | None = None,
         tools: list[ToolSpecRef] | None = None) -> ApiRequest:
    return ApiRequest(
        model="sim-model", system_prompt=system or [], messages=messages, tools=tools or []
    )


def _fc(call_id: str, name: str = "read_skill") -> ApiMessage:
    return ApiMessage(
        role="assistant", content="",
        tool_calls=[{"id": call_id, "type": "function",
                     "function": {"name": name, "arguments": "{}"}}],
    )


def test_ledger_records_in_order_and_last():
    ledger = RequestLedger()
    ledger.record(_req([ApiMessage(role="user", content="第一问")]))
    ledger.record(_req([ApiMessage(role="user", content="第二问")]))
    assert len(ledger.requests()) == 2
    last = ledger.last_request()
    assert last is not None
    assert last.message_texts("user") == ["第二问"]


def test_single_request_asserts_count():
    ledger = RequestLedger()
    with pytest.raises(AssertionError):
        ledger.single_request()  # 0 次
    ledger.record(_req([ApiMessage(role="user", content="唯一")]))
    assert ledger.single_request().message_texts("user") == ["唯一"]
    ledger.record(_req([ApiMessage(role="user", content="第二")]))
    with pytest.raises(AssertionError):
        ledger.single_request()  # 2 次


def test_saw_function_call_across_requests():
    ledger = RequestLedger()
    ledger.record(_req([ApiMessage(role="user", content="q")]))
    ledger.record(_req([
        ApiMessage(role="user", content="q"),
        _fc("c1"),
        ApiMessage(role="tool", content="结果文本", tool_call_id="c1"),
    ]))
    assert ledger.saw_function_call("c1")
    assert not ledger.saw_function_call("ghost")
    assert ledger.function_call_output_text("c1") == "结果文本"
    assert ledger.function_call_output_text("ghost") is None


def test_system_texts_merges_prompt_and_midstream():
    """system_prompt 各段 + 中段 system 消息合并保序。"""
    ledger = RequestLedger()
    ledger.record(_req(
        [ApiMessage(role="user", content="q"),
         ApiMessage(role="system", content="[Compacted history summary] s")],
        system=["entry skill body"],
    ))
    assert ledger.system_texts() == ["entry skill body", "[Compacted history summary] s"]


def test_tool_names_of_last_request():
    ledger = RequestLedger()
    assert ledger.tool_names() == set()
    ledger.record(_req(
        [ApiMessage(role="user", content="q")],
        tools=[ToolSpecRef(name="read_skill", description="d", input_schema={}),
               ToolSpecRef(name="call_skill", description="d", input_schema={})],
    ))
    assert ledger.tool_names() == {"read_skill", "call_skill"}


def test_blob_covers_system_and_messages():
    """blob 是 expect.must_contain 的匹配基底：system_prompt 与消息正文都要进。"""
    ledger = RequestLedger()
    rec = ledger.record(_req(
        [ApiMessage(role="user", content="用户正文")], system=["skill 正文"],
    ))
    assert "skill 正文" in rec.blob()
    assert "用户正文" in rec.blob()


def test_reset_clears_records_and_violations():
    from taifeng.llm.providers.sim.contract import SimContractViolation

    ledger = RequestLedger()
    ledger.record(_req([ApiMessage(role="user", content="q")]))
    ledger.violations.append(SimContractViolation("empty_messages", "x"))
    ledger.reset()
    assert ledger.requests() == []
    assert ledger.violations == []
