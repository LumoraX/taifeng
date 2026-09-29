"""SSE（``text/event-stream``）事件解析 + 续传游标（streamable HTTP 传输用）。

按 WHATWG HTML「Server-sent events § Interpreting an event stream」解析，只保留 MCP 用得到的部分：

| 行 | 处理 |
| --- | --- |
| 空行 | 派发事件：把事件的 ``id`` 提交为游标的最后事件 id；data 非空才产出 |
| ``:`` 开头 | 注释（keep-alive），忽略 |
| ``data:`` | 追加到 data 缓冲（多行以 ``\\n`` 拼接） |
| ``id:`` | 暂存事件 id（值含 NUL 忽略；空值 = 清空最后事件 id） |
| ``retry:`` | 全数字时立即更新 server 建议的重连间隔（毫秒），否则忽略 |
| 无冒号的行 | 字段名为整行、值为空 |

冒号后紧跟的**一个**空格属于分隔符，不进值。只带 ``id`` 不带 data 的事件（续传用的「起点」事件）
不产出任何 data，但其 id 照样提交——这是续传语义要求的。

续传（MCP 2025-06-18 §Transports「Resumability and Redelivery」）：server 可给 SSE 事件带 id；
流断后客户端 SHOULD 以 GET + ``Last-Event-ID: <最后收到的事件 id>`` 请求续传，server 可在同一条流上
重放其后的消息。``SseCursor`` 就是这条流的续传状态，跨重连复用。

与标准的一处差异：流**正常结束**时未以空行收尾的残余事件照常派发（标准要求丢弃）——旧实现即如此，
部分 server 最后一个事件后不补空行；流**中途断开**时残余事件不派发（其 data 可能不完整）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from taifeng.mcp.bridge import McpToolError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

EVENT_STREAM = "text/event-stream"
LAST_EVENT_ID_HEADER = "Last-Event-ID"

# 单次续传 / 重连等待上限（秒）：防 server 的 ``retry:`` 或指数退避把重连拖到分钟级
_MAX_RESUME_DELAY_SECONDS = 30.0
# 传输层失败统一映射的 JSON-RPC 错误码（实现自定义段，与 http_client 同段）
_TRANSPORT_ERROR = -32000


@dataclass
class SseCursor:
    """一条逻辑 SSE 流的续传状态（跨重连复用）。

    Attributes:
        last_event_id: 最后一个已派发事件的 id；空串表示 server 显式清空过。
        retry_ms: server 以 ``retry:`` 建议的重连间隔（毫秒）；未给为 None。
        events: 已派发的事件数（含只带 id 的事件），用于判断一次连接是否有进展。
    """

    last_event_id: str = ""
    retry_ms: int | None = None
    events: int = 0

    @property
    def resume_id(self) -> str | None:
        """可用于 ``Last-Event-ID`` 的事件 id；从未收到（或被清空）为 None。"""
        return self.last_event_id or None


@dataclass
class _EventBuffer:
    """正在拼装中的一个事件（空行时派发）。"""

    data: list[str] = field(default_factory=list)
    event_id: str | None = None
    touched: bool = False

    def dispatch(self, cursor: SseCursor) -> str | None:
        """派发：提交 id、计数，返回拼好的 data（没有 data 行则为 None）；随后清空缓冲。"""
        if not self.touched:
            return None
        if self.event_id is not None:
            cursor.last_event_id = self.event_id
        cursor.events += 1
        data = "\n".join(self.data) if self.data else None
        self.data, self.event_id, self.touched = [], None, False
        return data


def _split_field(line: str) -> tuple[str, str]:
    """``name: value`` → (name, value)；冒号后一个空格属分隔符；无冒号则值为空。"""
    name, sep, value = line.partition(":")
    if not sep:
        return line, ""
    return name, value[1:] if value.startswith(" ") else value


def _apply_field(buffer: _EventBuffer, cursor: SseCursor, name: str, value: str) -> None:
    """把一个字段应用到事件缓冲 / 游标；未知字段按标准忽略。"""
    if name == "data":
        buffer.data.append(value)
        buffer.touched = True
    elif name == "id":
        # 含 NUL 的 id 按标准忽略整个字段
        if "\0" not in value:
            buffer.event_id = value
            buffer.touched = True
    elif name == "retry":
        # 重连间隔即时生效，不随事件派发
        if value.isascii() and value.isdigit():
            cursor.retry_ms = int(value)
    elif name == "event":
        buffer.touched = True


async def iter_sse_data(response: httpx.Response, cursor: SseCursor) -> AsyncIterator[str]:
    """逐事件产出 data 文本，``id`` / ``retry`` 就地写入 ``cursor``。

    Args:
        response: 已确认为 ``text/event-stream`` 的流式响应。
        cursor: 该逻辑流的续传状态（续传时传同一个实例）。

    Yields:
        每个带 data 的事件的 data（多行 data 以换行拼接）。

    Raises:
        httpx.TransportError: 流中途断开（由调用方决定是否续传）；此时残余事件不派发。
    """
    buffer = _EventBuffer()
    async for line in response.aiter_lines():
        if line == "":
            data = buffer.dispatch(cursor)
            if data is not None:
                yield data
            continue
        if line.startswith(":"):
            continue
        name, value = _split_field(line)
        _apply_field(buffer, cursor, name, value)
    # 正常 EOF：未以空行收尾的最后一个事件照常派发（见模块说明的差异条目）
    data = buffer.dispatch(cursor)
    if data is not None:
        yield data


def is_event_stream(resp: httpx.Response) -> bool:
    """响应是否为 SSE 流（按 Content-Type 前缀判定，容许 ``; charset=`` 参数）。"""
    return str(resp.headers.get("content-type", "")).startswith(EVENT_STREAM)


def resume_delay(cursor: SseCursor, attempt: int, base_seconds: float) -> float:
    """第 ``attempt``（从 1 起）次续传 / 重连前的等待秒数。

    server 以 ``retry:`` 给过重连间隔则用它（SSE 标准语义），否则 ``base_seconds`` 按次数翻倍；
    单次不超过 30 秒。
    """
    if cursor.retry_ms is not None:
        delay = cursor.retry_ms / 1000
    else:
        delay = base_seconds * 2 ** (attempt - 1)
    return min(delay, _MAX_RESUME_DELAY_SECONDS)


async def require_resumed_stream(resp: httpx.Response) -> None:
    """续传 GET 必须回 SSE 流；405 = server 不支持续传，其余错误同样显式失败。

    Raises:
        McpToolError: 405 / 其他 4xx-5xx / 非 SSE 响应（code -32000）。
    """
    if resp.status_code == 405:
        raise McpToolError(
            _TRANSPORT_ERROR,
            "SSE stream broke and the server does not support resumption (GET returned 405)")
    if resp.status_code >= 400:
        body = (await resp.aread()).decode("utf-8", errors="replace")[:200]
        raise McpToolError(
            _TRANSPORT_ERROR, f"SSE stream resumption rejected: http {resp.status_code}: {body}")
    if not is_event_stream(resp):
        raise McpToolError(
            _TRANSPORT_ERROR,
            f"SSE stream resumption returned {resp.headers.get('content-type')!r}, "
            f"expected {EVENT_STREAM}")


__all__ = [
    "EVENT_STREAM",
    "LAST_EVENT_ID_HEADER",
    "SseCursor",
    "is_event_stream",
    "iter_sse_data",
    "require_resumed_stream",
    "resume_delay",
]
