"""RoutingSimClient 单测 —— 标记路由语义继承 + conformance 行为叠加。"""

from __future__ import annotations

import pytest

from taifeng.llm.providers.sim import (
    RoutingSimClient,
    SimContractViolation,
    SimScriptExhausted,
    SimTurn,
)
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken


def _req(text: str, *, system: list[str] | None = None) -> ApiRequest:
    return ApiRequest(
        model="m", system_prompt=system or [],
        messages=[ApiMessage(role="user", content=text)],
    )


async def _drain_text(client: RoutingSimClient, request: ApiRequest) -> str:
    out = ""
    async with client.session(cancel=CancellationToken()) as s:
        async for ev in s.stream(request):
            if ev.kind == "text_delta":
                out += ev.data.get("text", "")
    return out


async def test_routes_by_marker_regardless_of_order():
    """标记 A/B 各自路由；逆序调用结果不变（并发顺序无关）。"""
    client = RoutingSimClient(routes={
        "ROUTE_A": [SimTurn(text="answer-A")],
        "ROUTE_B": [SimTurn(text="answer-B")],
    })
    assert await _drain_text(client, _req("do ROUTE_B now")) == "answer-B"
    assert await _drain_text(client, _req("do ROUTE_A now")) == "answer-A"


async def test_marker_in_system_prompt_matches():
    """skill body 在 system_prompt 内 —— 标记须能命中两处。"""
    client = RoutingSimClient(routes={"EXPERT_MARKER": [SimTurn(text="hit")]})
    assert await _drain_text(
        client, _req("普通输入", system=["专科 skill 正文 EXPERT_MARKER"])
    ) == "hit"


async def test_per_marker_cursor_advances():
    """同一标记多次命中按列表顺序推进（每标记独立游标）。"""
    client = RoutingSimClient(routes={"R": [SimTurn(text="一"), SimTurn(text="二")]})
    assert await _drain_text(client, _req("R")) == "一"
    assert await _drain_text(client, _req("R")) == "二"


async def test_cursor_exhausted_raises():
    """游标越界抛 SimScriptExhausted（替代旧静默空 turn）。"""
    client = RoutingSimClient(routes={"R": [SimTurn(text="仅一条")]})
    await _drain_text(client, _req("R"))
    with pytest.raises(SimScriptExhausted):
        await _drain_text(client, _req("R"))


async def test_unmatched_marker_raises_keyerror():
    """无标记命中 → KeyError（禁 silent fallback，语义与旧 RoutingMock 一致）。"""
    client = RoutingSimClient(routes={"R": [SimTurn(text="x")]})
    with pytest.raises(KeyError):
        await _drain_text(client, _req("no marker here"))


async def test_conformance_applies_to_routing():
    """conformance 行为叠加：违规请求同样被拦 + 记账。"""
    client = RoutingSimClient(routes={"R": [SimTurn(text="x")]})
    with pytest.raises(SimContractViolation):
        await _drain_text(client, ApiRequest(model="m", messages=[]))
    assert client.ledger.violations[0].rule == "empty_messages"


async def test_reset_restores_cursors():
    client = RoutingSimClient(routes={"R": [SimTurn(text="一")]})
    await _drain_text(client, _req("R"))
    client.reset()
    assert await _drain_text(client, _req("R")) == "一"
