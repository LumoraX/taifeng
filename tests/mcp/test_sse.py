"""SSE 事件解析与续传游标（sse.py）。

按 WHATWG「Interpreting an event stream」：空行派发、``:`` 注释、``id`` 在派发时提交、
``retry`` 全数字才生效；只带 id 的事件不产出 data 但提交 id；中途断开的残余事件不派发。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from taifeng.mcp.bridge import McpToolError
from taifeng.mcp.sse import SseCursor, iter_sse_data, require_resumed_stream, resume_delay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _collect(body: bytes, cursor: SseCursor | None = None) -> tuple[list[str], SseCursor]:
    cursor = cursor or SseCursor()
    resp = httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    return [data async for data in iter_sse_data(resp, cursor)], cursor


async def test_fields_comments_and_multiline_data() -> None:
    """多行 data 以换行拼接；注释忽略；冒号后一个空格属分隔符；无冒号行值为空。"""
    body = (b": keep-alive\n"
            b"event: message\ndata: {\"a\":\ndata:  1}\n\n"
            b"data\n\n")
    events, cursor = await _collect(body)
    assert events == ['{"a":\n 1}', ""]
    assert cursor.events == 2
    assert cursor.resume_id is None


async def test_id_committed_on_dispatch_and_persists() -> None:
    """id 在派发时提交；后续不带 id 的事件不改它；只带 id 的事件不产出 data 但提交 id。"""
    body = b"id: e1\ndata: one\n\ndata: two\n\nid: e2\n\n"
    events, cursor = await _collect(body)
    assert events == ["one", "two"]
    assert cursor.last_event_id == "e2"
    assert cursor.events == 3


@pytest.mark.parametrize(("body", "expected"), [
    (b"id: e1\ndata: x\n\nid\ndata: y\n\n", None),        # 空 id = 清空，此后无从续传
    (b"id: e1\ndata: x\n\nid: bad\0id\ndata: y\n\n", "e1"),  # 含 NUL 的 id 整个字段忽略
])
async def test_empty_and_nul_ids(body: bytes, expected: str | None) -> None:
    """空 id 清空最后事件 id；含 NUL 的 id 被忽略。"""
    _, cursor = await _collect(body)
    assert cursor.resume_id == expected


async def test_retry_only_accepts_digits() -> None:
    """retry 全数字才更新重连间隔（即时生效，不随事件）；否则忽略。"""
    _, cursor = await _collect(b"retry: 1500\n\nretry: soon\n\n")
    assert cursor.retry_ms == 1500


async def test_trailing_event_at_clean_eof_is_dispatched() -> None:
    """正常 EOF 时未以空行收尾的最后一个事件照常派发（兼容不补空行的 server）。"""
    events, cursor = await _collect(b"id: e9\ndata: tail")
    assert events == ["tail"]
    assert cursor.last_event_id == "e9"


async def test_broken_stream_drops_partial_event() -> None:
    """中途断开：残余事件不派发、其 id 不提交；断开以 TransportError 上抛交调用方处理。"""

    async def _body() -> AsyncIterator[bytes]:
        yield b"id: e1\ndata: whole\n\nid: e2\ndata: {\"trunc"
        raise httpx.ReadError("connection reset")

    cursor = SseCursor()
    resp = httpx.Response(200, content=_body(), headers={"content-type": "text/event-stream"})
    seen: list[str] = []
    with pytest.raises(httpx.ReadError):
        async for data in iter_sse_data(resp, cursor):
            seen.append(data)
    assert seen == ["whole"]
    assert cursor.last_event_id == "e1"


def test_resume_delay_prefers_server_retry_then_backoff_with_cap() -> None:
    """server 的 retry 优先；否则基础间隔按次数翻倍；单次不超过 30 秒。"""
    assert resume_delay(SseCursor(retry_ms=250), 5, 1.0) == 0.25
    assert [resume_delay(SseCursor(), n, 0.5) for n in (1, 2, 3)] == [0.5, 1.0, 2.0]
    assert resume_delay(SseCursor(), 10, 1.0) == 30.0
    assert resume_delay(SseCursor(retry_ms=600_000), 1, 1.0) == 30.0


@pytest.mark.parametrize(("status", "ctype", "match"), [
    (405, "text/plain", "does not support resumption"),
    (404, "text/plain", "resumption rejected: http 404"),
    (200, "application/json", "expected text/event-stream"),
])
async def test_require_resumed_stream_rejects(status: int, ctype: str, match: str) -> None:
    """续传 GET：405 / 错误状态 / 非 SSE → 显式 McpToolError。"""
    resp = httpx.Response(status, content=b"nope", headers={"content-type": ctype})
    with pytest.raises(McpToolError, match=match):
        await require_resumed_stream(resp)


async def test_require_resumed_stream_accepts_event_stream() -> None:
    """200 + text/event-stream（可带 charset）→ 通过。"""
    resp = httpx.Response(200, content=b"", headers={
        "content-type": "text/event-stream; charset=utf-8"})
    await require_resumed_stream(resp)
