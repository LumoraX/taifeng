"""anthropic-cache —— 尾部滚动缓存断点 + CacheBreakpoint.ttl_seconds 透传。

回归点：ttl_seconds 此前是死字段（恒 5 分钟）；只在 cache anchor 打一个标记，anchor 之后
逐轮变长的尾部每次都按全价计费。
"""

from __future__ import annotations

from typing import Any

import pytest

from taifeng.llm.errors import InvalidRequestError
from taifeng.llm.providers.anthropic_provider import AnthropicClient, AnthropicSession
from taifeng.llm.types import ApiMessage, ApiRequest, CacheBreakpoint
from taifeng.loop.cancellation import CancellationToken


def _session(**kwargs: Any) -> AnthropicSession:
    return AnthropicSession(api_key="k", model="claude", base_url="https://x",
                            cancel=CancellationToken(), **kwargs)


def _request(*breakpoints: CacheBreakpoint) -> ApiRequest:
    return ApiRequest(model="claude", system_prompt=["SYS"], messages=[
        ApiMessage(role="user", content="写报告"),
        ApiMessage(role="assistant", content="",
                   tool_calls=[{"id": "t1", "function": {"name": "w", "arguments": "{}"}}]),
        ApiMessage(role="tool", content="结果", tool_call_id="t1"),
    ], cache_breakpoints=list(breakpoints))


def _marks(payload: dict[str, Any]) -> list[tuple[int, str, dict[str, Any]]]:
    """(消息序号, 块类型, cache_control) 列表。"""
    return [(i, b["type"], b["cache_control"])
            for i, m in enumerate(payload["messages"]) for b in m["content"]
            if "cache_control" in b]


def test_tail_breakpoint_added_after_anchor() -> None:
    """anchor 标记 + 最后一条消息（tool_result）上的尾部标记，共两个。"""
    payload = _session()._build_payload(_request(CacheBreakpoint(index=0)))
    assert _marks(payload) == [
        (0, "text", {"type": "ephemeral"}),
        (2, "tool_result", {"type": "ephemeral"}),
    ]


def test_tail_breakpoint_alone_without_anchor() -> None:
    """首轮无 anchor 时只有尾部标记（缓存 system + tools + 全部消息）。"""
    assert _marks(_session()._build_payload(_request())) == [
        (2, "tool_result", {"type": "ephemeral"})]


def test_tail_disabled_keeps_anchor_only() -> None:
    payload = _session(cache_tail=False)._build_payload(_request(CacheBreakpoint(index=0)))
    assert _marks(payload) == [(0, "text", {"type": "ephemeral"})]


def test_tail_on_anchor_message_not_duplicated() -> None:
    """anchor 恰为最后一条时不重复打标记。"""
    payload = _session()._build_payload(_request(CacheBreakpoint(index=2)))
    assert _marks(payload) == [(2, "tool_result", {"type": "ephemeral"})]


def test_one_hour_ttl_from_breakpoint_applies_to_every_mark() -> None:
    """断点声明 3600 → 所有标记（含尾部）为 1h。"""
    payload = _session()._build_payload(_request(CacheBreakpoint(index=0, ttl_seconds=3600)))
    assert {m[2]["ttl"] for m in _marks(payload)} == {"1h"}


def test_client_ttl_override_wins() -> None:
    payload = _session(cache_ttl_seconds=3600)._build_payload(_request(CacheBreakpoint(index=0)))
    assert [m[2] for m in _marks(payload)] == [{"type": "ephemeral", "ttl": "1h"}] * 2


@pytest.mark.parametrize("ttl", [60, 600, 86_400])
def test_unsupported_ttl_rejected(ttl: int) -> None:
    """Anthropic 只有 5m / 1h 两档：其他值显式报错，不就近取整。"""
    with pytest.raises(InvalidRequestError, match="300 or 3600"):
        _session()._build_payload(_request(CacheBreakpoint(index=0, ttl_seconds=ttl)))
    with pytest.raises(InvalidRequestError, match="300 or 3600"):
        AnthropicClient(api_key="k", cache_ttl_seconds=ttl)


def test_mixed_ttls_rejected() -> None:
    req = _request(CacheBreakpoint(index=0), CacheBreakpoint(index=1, ttl_seconds=3600))
    with pytest.raises(InvalidRequestError, match="one ttl"):
        _session()._build_payload(req)


def test_thinking_block_never_marked() -> None:
    """最后一条消息的末块是 thinking 时，标记落到之前的可标记块。"""
    from taifeng.llm.providers.anthropic_cache import mark_tail

    messages = [{"role": "assistant", "content": [
        {"type": "text", "text": "a"}, {"type": "thinking", "thinking": "t", "signature": "s"}]}]
    mark_tail(messages, {"type": "ephemeral"})
    assert "cache_control" in messages[0]["content"][0]
    assert "cache_control" not in messages[0]["content"][1]
