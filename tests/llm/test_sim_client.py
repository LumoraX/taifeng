"""SimClient 单测 —— 回放骨架 / strict 耗尽 / 分片 / finish / 故障注入 / 违规记账。"""

from __future__ import annotations

import json

import pytest

from taifeng.llm.errors import (
    ContentFilterError,
    ContextOverflowError,
    RateLimitError,
    ServerError,
)
from taifeng.llm.providers.sim import (
    SimClient,
    SimContractViolation,
    SimExpect,
    SimFault,
    SimScriptExhausted,
    SimTurn,
)
from taifeng.llm.types import ApiMessage, ApiRequest, ToolSpecRef
from taifeng.loop.cancellation import CancellationToken


def _req(text: str = "你好", *, tools: list[str] | None = None) -> ApiRequest:
    return ApiRequest(
        model="m",
        messages=[ApiMessage(role="user", content=text)],
        tools=[ToolSpecRef(name=n, description="d", input_schema={}) for n in (tools or [])],
    )


async def _drain(client: SimClient, request: ApiRequest) -> list:
    """收集一次采样的全部事件。"""
    events = []
    async with client.session(cancel=CancellationToken()) as s:
        async for ev in s.stream(request):
            events.append(ev)
    return events


async def test_event_skeleton_order():
    """事件骨架：created → server_model → text_delta* → prompt_cache → completed。"""
    client = SimClient(turns=[SimTurn(text="十六个字符的答复正文内容在此处")])
    events = await _drain(client, _req())
    kinds = [ev.kind for ev in events]
    assert kinds[0] == "created"
    assert kinds[1] == "server_model"
    assert "text_delta" in kinds
    # prompt_cache 每 turn 必发（账本自动折算），completed 收尾
    assert kinds[-2] == "prompt_cache"
    assert kinds[-1] == "completed"
    text = "".join(ev.data.get("text", "") for ev in events if ev.kind == "text_delta")
    assert text == "十六个字符的答复正文内容在此处"


async def test_script_exhausted_raises():
    """脚本耗尽抛 SimScriptExhausted —— 多采样一次立刻暴露（不再静默空 turn）。"""
    client = SimClient(turns=[SimTurn(text="唯一")])
    await _drain(client, _req())
    with pytest.raises(SimScriptExhausted):
        await _drain(client, _req("再来"))


async def test_tool_call_chunked_delta_then_done():
    """arguments > 16 字符 → ≥2 个 tool_call_delta（首片带 name）+ done 重组一致。"""
    arguments = json.dumps({"skill_id": "metabolic-analysis", "depth": 3})
    client = SimClient(turns=[SimTurn(tool_calls=[
        {"id": "c1", "name": "call_skill", "arguments": arguments},
    ])])
    events = await _drain(client, _req(tools=["call_skill"]))
    deltas = [ev for ev in events if ev.kind == "tool_call_delta"]
    dones = [ev for ev in events if ev.kind == "tool_call_done"]
    assert len(deltas) >= 2
    assert deltas[0].data["name"] == "call_skill"
    assert deltas[1].data.get("name") is None  # 后续分片不带 name（OpenAI 语义）
    assert "".join(d.data["delta"] for d in deltas) == arguments
    assert len(dones) == 1
    assert dones[0].data["arguments"] == arguments


async def test_chunked_disabled_falls_back_to_done_only():
    """chunked_tool_calls=False → 退化为一次性 done（降噪开关）。"""
    client = SimClient(
        turns=[SimTurn(tool_calls=[{"id": "c1", "name": "read_skill", "arguments": "{}"}])],
        chunked_tool_calls=False,
    )
    events = await _drain(client, _req(tools=["read_skill"]))
    assert [ev.kind for ev in events if ev.kind.startswith("tool_call")] == ["tool_call_done"]


async def test_finish_semantics():
    """finish 显式声明覆盖默认推导；content_filter 镜像真实 provider 抛错。"""
    # 默认推导：有 tool_calls → end_turn=False
    client = SimClient(turns=[
        SimTurn(tool_calls=[{"id": "c1", "name": "read_skill", "arguments": "{}"}]),
        SimTurn(text="收尾", finish="tool_use"),  # 显式不结束
        SimTurn(text="结束", finish="end_turn"),
    ])
    ev1 = await _drain(client, _req(tools=["read_skill"]))
    assert ev1[-1].data["end_turn"] is False
    ev2 = await _drain(client, _req("继续"))
    assert ev2[-1].data["end_turn"] is False
    ev3 = await _drain(client, _req("再继续"))
    assert ev3[-1].data["end_turn"] is True


async def test_content_filter_finish_raises():
    client = SimClient(turns=[SimTurn(text="", finish="content_filter")])
    with pytest.raises(ContentFilterError):
        await _drain(client, _req())


async def test_fault_rate_limit_and_server_error():
    """前置故障：产出任何事件前直接抛；下一脚本 turn 可作为重试结果消费。"""
    client = SimClient(turns=[
        SimTurn(fault=SimFault.rate_limit(retry_after_seconds=2.0)),
        SimTurn(fault=SimFault.server_error()),
        SimTurn(text="重试成功"),
    ])
    with pytest.raises(RateLimitError) as ei:
        await _drain(client, _req())
    assert ei.value.retry_after_seconds == 2.0
    with pytest.raises(ServerError):
        await _drain(client, _req())
    events = await _drain(client, _req())
    assert events[-1].kind == "completed"


async def test_fault_malformed_arguments():
    """畸形参数：arguments 为非法 JSON（engine 坏参处置路径）。"""
    client = SimClient(turns=[SimTurn(
        tool_calls=[{"id": "c1", "name": "read_skill", "arguments": '{"k": "v"}'}],
        fault=SimFault.malformed_arguments(),
    )])
    events = await _drain(client, _req(tools=["read_skill"]))
    done = next(ev for ev in events if ev.kind == "tool_call_done")
    with pytest.raises(json.JSONDecodeError):
        json.loads(done.data["arguments"])


async def test_fault_truncate_stream_no_completed():
    """截断流：产满 N 个事件后终止，无 completed（半途崩溃恢复路径）。"""
    client = SimClient(turns=[SimTurn(
        text="很长的正文" * 10, fault=SimFault.truncate_stream(after_events=3),
    )])
    events = await _drain(client, _req())
    assert len(events) == 3
    assert all(ev.kind != "completed" for ev in events)


async def test_contract_violation_recorded_and_raised():
    """合同违规：上抛 + 记入 ledger.violations（双保险）。"""
    client = SimClient(turns=[SimTurn(text="x")])
    bad = ApiRequest(model="m", messages=[])  # empty_messages
    with pytest.raises(SimContractViolation):
        await _drain(client, bad)
    assert len(client.ledger.violations) == 1
    assert client.ledger.violations[0].rule == "empty_messages"
    # 违规请求不消耗脚本游标
    events = await _drain(client, _req())
    assert events[-1].kind == "completed"


async def test_response_side_unknown_tool_recorded():
    """脚本要调的工具没注册进请求 → 违规记账 + 上抛。"""
    client = SimClient(turns=[SimTurn(
        tool_calls=[{"id": "c1", "name": "call_skill", "arguments": "{}"}],
    )])
    with pytest.raises(SimContractViolation):
        await _drain(client, _req(tools=["read_skill"]))  # 只注册了 read_skill
    assert client.ledger.violations[0].rule == "unknown_tool_response"


async def test_expect_must_include_output_recorded():
    """SimTurn.expect 违规：缺工具结果 → expect_missing_output。"""
    client = SimClient(turns=[SimTurn(
        text="x", expect=SimExpect(must_include_output_for=("c-gone",)),
    )])
    with pytest.raises(SimContractViolation):
        await _drain(client, _req())
    assert client.ledger.violations[0].rule == "expect_missing_output"


async def test_context_window_overflow_propagates():
    """超窗抛 ContextOverflowError（LLMError 体系，走 engine 自愈路径）。"""
    client = SimClient(turns=[SimTurn(text="x")], context_window=10)
    with pytest.raises(ContextOverflowError):
        await _drain(client, _req("超长内容" * 50))


async def test_explicit_cache_read_overrides_ledger():
    """SimTurn.cache_read 显式赋值覆写账本自动折算（MockTurn 兼容语义）。"""
    client = SimClient(turns=[SimTurn(text="x", cache_read=77, cache_creation=3)])
    events = await _drain(client, _req())
    pc = next(ev for ev in events if ev.kind == "prompt_cache")
    assert pc.data["cache_read_input_tokens"] == 77
    assert pc.data["cache_creation_input_tokens"] == 3


async def test_ledger_records_requests():
    """请求侦察：ledger 记录全部采样请求。"""
    client = SimClient(turns=[SimTurn(text="a"), SimTurn(text="b")])
    await _drain(client, _req("第一问"))
    await _drain(client, _req("第二问"))
    assert len(client.ledger.requests()) == 2
    assert client.ledger.message_texts("user") == ["第二问"]


async def test_reset_restores_everything():
    """reset 复位游标 + ledger + 账本 + 信号。"""
    client = SimClient(turns=[SimTurn(text="a")])
    await _drain(client, _req())
    client.reset()
    events = await _drain(client, _req())
    assert events[-1].kind == "completed"
    assert len(client.ledger.requests()) == 1
