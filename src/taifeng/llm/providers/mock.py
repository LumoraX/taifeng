"""Mock ModelClient —— 用于单元测试与示例，无需任何 API key。

按预定义脚本逐 turn 产出 ``ResponseEvent`` 序列。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from taifeng.llm.client import ModelClient
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    prompt_cache,
    reasoning_delta,
    server_model,
    structured_output,
    text_delta,
    tool_call_done,
)
from taifeng.llm.types import ApiRequest, TokenUsage
from taifeng.loop.cancellation import CancellationToken


@dataclass
class MockTurn:
    """单个 turn 的脚本。"""

    text: str = ""
    reasoning: str = ""
    """thinking 模型 reasoning 回放(reasoning-content-passback):非空时在 text
    之前 emit ``reasoning_delta``(与真实 provider 产出顺序一致);默认空=零变化。"""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """每项形如 ``{"id": ..., "name": ..., "arguments": "..."}``"""
    usage: TokenUsage = field(default_factory=lambda: TokenUsage(input_tokens=100, output_tokens=50))
    delay_seconds: float = 0.0
    structured: dict[str, Any] | list[Any] | None = None
    """structured_output 回放数据 —— 仅当本 turn 对应的 ApiRequest.response_format
    非 None 时 emit；否则忽略本字段。"""
    cache_read: int | None = None
    """prompt_cache 回放：cache_read_input_tokens；None → 不 emit prompt_cache 事件。"""
    cache_creation: int = 0
    """prompt_cache 回放：cache_creation_input_tokens。"""
    request_id: str | None = None
    """G3：completed 事件回放的服务端 request-id。"""


class MockSession:
    def __init__(self, turn: MockTurn, cancel: CancellationToken, model: str) -> None:
        self._turn = turn
        self._cancel = cancel
        self._model = model

    async def __aenter__(self) -> MockSession:
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        yield created()
        yield server_model(self._model)
        if self._turn.delay_seconds:
            await asyncio.sleep(self._turn.delay_seconds)
        self._cancel.raise_if_cancelled()
        # reasoning 先于 text(与真实 thinking provider 产出顺序一致),按 8-char 切分
        r_text = self._turn.reasoning
        for i in range(0, len(r_text), 8):
            self._cancel.raise_if_cancelled()
            yield reasoning_delta(r_text[i:i + 8])
        # 文本：按 4-char 切分模拟流式
        text = self._turn.text
        for i in range(0, len(text), 8):
            self._cancel.raise_if_cancelled()
            yield text_delta(text[i:i + 8])
        # 工具调用一次性给（mock 简化）
        for tc in self._turn.tool_calls:
            yield tool_call_done(
                call_id=tc["id"],
                name=tc["name"],
                arguments=tc.get("arguments", "{}"),
            )
        # structured_output：仅当请求带 response_format 且 turn 配置了 structured 回放数据时 emit
        if request.response_format is not None and self._turn.structured is not None:
            raw_text = json.dumps(self._turn.structured, ensure_ascii=False)
            yield structured_output(parsed=self._turn.structured, raw_text=raw_text)
        # prompt_cache：仅当本 turn 显式配置 cache_read 时 emit（默认 provider 才有）
        if self._turn.cache_read is not None:
            yield prompt_cache(
                cache_read=self._turn.cache_read,
                cache_creation=self._turn.cache_creation,
            )
        yield completed(
            response_id="mock-resp",
            usage=self._turn.usage,
            end_turn=not bool(self._turn.tool_calls),
            request_id=self._turn.request_id,
        )


class MockClient(ModelClient):
    """按预设 ``MockTurn`` 列表逐次回放。

    示例：
        >>> client = MockClient(turns=[
        ...     MockTurn(text="先调用工具", tool_calls=[
        ...         {"id": "c1", "name": "read_skill", "arguments": '{"skill_id": "x"}'},
        ...     ]),
        ...     MockTurn(text="工具返回后的最终回复。"),
        ... ])
    """

    def __init__(self, *, turns: list[MockTurn], model: str = "mock-model") -> None:
        self._turns = list(turns)
        self._model = model
        self._idx = 0

    def session(self, *, cancel: CancellationToken, model: str | None = None) -> MockSession:
        if self._idx >= len(self._turns):
            # 默认空回复，避免测试越界
            turn = MockTurn(text="")
        else:
            turn = self._turns[self._idx]
            self._idx += 1
        return MockSession(turn=turn, cancel=cancel, model=model or self._model)

    def reset(self) -> None:
        self._idx = 0


class _RoutingMockSession:
    """延迟到 stream() 才按 request 文本选脚本的 session。

    与 ``MockSession`` 区别：turn 不在构造期定，而在 ``stream()`` 拿到 request 后，
    按 routes 里的标记子串匹配选取。这样并发子 turn 调 ``session()`` 的顺序不再
    影响回放结果（顺序无关）。
    """

    def __init__(self, client: RoutingMockClient, cancel: CancellationToken, model: str) -> None:
        self._client = client
        self._cancel = cancel
        self._model = model

    async def __aenter__(self) -> _RoutingMockSession:
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        # 把 system_prompt（skill body 在此）+ 所有消息文本拼成一个串，找首个命中标记。
        # 关键：skill body 进 ApiRequest.system_prompt 而非 messages，标记需覆盖两处。
        parts = list(request.system_prompt)
        parts.extend(str(getattr(m, "content", "")) for m in request.messages)
        blob = " ".join(parts)
        turn = self._client._next_turn_for(blob)  # noqa: SLF001
        # 复用 MockSession 的事件产出逻辑，避免重复实现流式协议
        inner = MockSession(turn=turn, cancel=self._cancel, model=self._model)
        async with inner as s:
            async for ev in s.stream(request):
                yield ev


class RoutingMockClient(ModelClient):
    """按请求消息标记路由的 Mock —— 并发场景回放确定。

    ``routes``: ``{marker_substring: [MockTurn, ...]}``。``stream()`` 时把请求消息
    文本拼成一个串，按 routes 插入顺序找首个作为子串命中的标记，取其脚本列表；同一
    标记被多次命中时按列表顺序推进（每标记独立游标）。无任何标记命中 → ``KeyError``
    （禁 silent fallback，符合仓库测试约束）。

    示例：
        >>> client = RoutingMockClient(routes={
        ...     "ROUTE_A": [MockTurn(text="A done")],
        ...     "ROUTE_B": [MockTurn(text="B done")],
        ... })
    """

    def __init__(self, *, routes: dict[str, list[MockTurn]], model: str = "mock-model") -> None:
        self._routes = {k: list(v) for k, v in routes.items()}
        self._cursors: dict[str, int] = dict.fromkeys(routes, 0)
        self._model = model

    def session(
        self, *, cancel: CancellationToken, model: str | None = None
    ) -> _RoutingMockSession:
        return _RoutingMockSession(self, cancel, model or self._model)

    def _next_turn_for(self, blob: str) -> MockTurn:
        """按 routes 插入顺序找首个命中标记，推进其游标并返回对应 turn。"""
        for marker, turns in self._routes.items():
            if marker in blob:
                idx = self._cursors[marker]
                self._cursors[marker] = idx + 1
                # 越界 → 空回复（脚本耗尽时不报错，与 MockClient 行为一致）
                return turns[idx] if idx < len(turns) else MockTurn(text="")
        raise KeyError(f"RoutingMockClient: 无标记命中, blob={blob[:120]!r}")

    def reset(self) -> None:
        self._cursors = dict.fromkeys(self._routes, 0)
