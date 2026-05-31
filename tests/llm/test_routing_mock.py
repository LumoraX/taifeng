"""RoutingMockClient —— 按请求消息标记路由,验证回放与调用顺序无关。"""
from __future__ import annotations

import pytest

from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken


def _req(text: str) -> ApiRequest:
    """构造一个 user 消息含 text 的最小 ApiRequest。"""
    return ApiRequest(model="m", messages=[ApiMessage(role="user", content=text)])


async def _drain_text(client: RoutingMockClient, text: str) -> str:
    sess = client.session(cancel=CancellationToken())
    out = ""
    async with sess as s:
        async for ev in s.stream(_req(text)):
            if ev.kind == "text_delta":
                out += ev.data.get("text", "")
    return out


async def test_routes_by_marker_regardless_of_order() -> None:
    """标记 A/B 各自路由到对应脚本;先取 B 再取 A 也对。"""
    client = RoutingMockClient(
        routes={
            "ROUTE_A": [MockTurn(text="answer-A")],
            "ROUTE_B": [MockTurn(text="answer-B")],
        }
    )
    # 逆序调用,验证与顺序无关
    assert await _drain_text(client, "do ROUTE_B now") == "answer-B"
    assert await _drain_text(client, "do ROUTE_A now") == "answer-A"


async def test_unmatched_marker_raises() -> None:
    """无任何标记命中 → 抛 KeyError(测试基建禁 silent fallback)。"""
    client = RoutingMockClient(routes={"ROUTE_A": [MockTurn(text="x")]})
    with pytest.raises(KeyError):
        await _drain_text(client, "no marker here")
