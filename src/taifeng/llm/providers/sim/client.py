"""SimClient / RoutingSimClient —— conformance 模拟器的 ModelClient 组装层。

把 contract（审请求）→ server（记账）→ script（按剧本作答）串成完整服务端行为：

1. 登记请求进 ``RequestLedger``；
2. 协议合同校验（违规抛 ``SimContractViolation`` 并记入 ledger —— D4 双保险）；
3. 选脚本（耗尽抛 ``SimScriptExhausted``，绝不静默吐空 turn）；
4. 故障注入（rate_limit / server_error 在产出任何事件前直接抛）；
5. 响应侧反查 + 逐 turn expect 断言；
6. token 记账（超窗抛 ``ContextOverflowError`` 走 engine 真实自愈路径）
   + 前缀 cache 账本自动折算 prompt_cache；
7. 时序编排（await_signal / emit_signal / delay）后按全保真流式产出事件
   （text 8-char 切片、tool_call arguments 16-char delta 分片 + done 收尾）。

与旧 ``MockClient`` 的回放语义兼容点：事件骨架同构
（created → server_model → text_delta* → tool_call* → structured_output? →
prompt_cache → completed）、``CancellationToken`` 检查点保留（R4）。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from taifeng.llm.errors import ContentFilterError, RateLimitError, ServerError
from taifeng.llm.events import (
    completed,
    created,
    prompt_cache,
    server_model,
    structured_output,
    text_delta,
    tool_call_delta,
    tool_call_done,
)
from taifeng.llm.providers.sim.contract import (
    RequestContractValidator,
    SimContractViolation,
)
from taifeng.llm.providers.sim.script import SimScriptExhausted, SimTurn
from taifeng.llm.providers.sim.server import (
    RequestLedger,
    SimServerState,
    _canonical_text,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken

# 流式切片粒度：text 沿用旧 mock 的 8 字符；tool_call arguments 16 字符
# （保证常规 arguments 至少产出 ≥2 个 delta 分片，让 engine 的重组路径真实工作）
_TEXT_CHUNK = 8
_ARGS_CHUNK = 16

# malformed_arguments 故障注入的非法 JSON 片段（截断的对象字面量）
_MALFORMED_ARGS = '{"broken": '


class SimCoordinator:
    """确定性并发时序协调器（信号量字典）。

    turn 声明 ``await_signal`` → 开始产出前等待该信号；
    turn 声明 ``emit_signal`` → completed 之前点亮该信号。
    测试据此显式编排并发完成顺序（确定性、零随机）。
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def _event(self, name: str) -> asyncio.Event:
        """按名取信号（不存在即创建——允许先 wait 后 signal 的任意时序）。"""
        if name not in self._events:
            self._events[name] = asyncio.Event()
        return self._events[name]

    async def wait(self, name: str) -> None:
        """等待信号点亮。"""
        await self._event(name).wait()

    def signal(self, name: str) -> None:
        """点亮信号（幂等）。"""
        self._event(name).set()

    def reset(self) -> None:
        """清空全部信号。"""
        self._events.clear()


class _SimSession:
    """turn 级 session：实际的 审请求 → 记账 → 回放 流水线。"""

    def __init__(self, client: _SimClientBase, cancel: CancellationToken, model: str) -> None:
        self._client = client
        self._cancel = cancel
        self._model = model

    async def __aenter__(self) -> _SimSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        client = self._client
        record = client.ledger.record(request)

        # 1. 请求侧合同校验（违规记 ledger 后上抛 —— 双保险）
        try:
            client.validator.validate(request)
        except SimContractViolation as violation:
            client.ledger.violations.append(violation)
            raise

        # 2. 选脚本（耗尽抛 SimScriptExhausted；选取失败不推进游标）
        turn = client._next_turn(request)  # noqa: SLF001

        # 3. 前置故障：限速 / 服务端错误在产出任何事件前直接抛（与真实服务端一致）
        if turn.fault is not None and turn.fault.kind == "rate_limit":
            raise RateLimitError(
                "sim: rate limited", retry_after_seconds=turn.fault.retry_after_seconds
            )
        if turn.fault is not None and turn.fault.kind == "server_error":
            raise ServerError("sim: internal server error")

        # 4. 响应侧反查 + 逐 turn expect 断言
        try:
            client.validator.validate_response_side(turn, request)
            client.validator.validate_expect(turn, record)
        except SimContractViolation as violation:
            client.ledger.violations.append(violation)
            raise

        # 5. 记账：超窗抛 ContextOverflowError（LLMError，走 engine 自愈路径）
        cache_read, cache_creation = client.server.admit(request)
        if turn.cache_read is not None:
            # 旧脚本手填值覆写自动账本（MockTurn 兼容语义）
            cache_read, cache_creation = turn.cache_read, turn.cache_creation

        # 6. 时序编排：先等信号，再模拟推理延迟
        if turn.await_signal is not None:
            await client.coordinator.wait(turn.await_signal)
        if turn.delay_seconds:
            await asyncio.sleep(turn.delay_seconds)
        self._cancel.raise_if_cancelled()

        # 7. 全保真流式回放（truncate_stream 故障 → 产满 N 个事件后无声终止）
        budget: int | None = None
        if turn.fault is not None and turn.fault.kind == "truncate_stream":
            budget = turn.fault.after_events
        for emitted, ev in enumerate(
            self._compose_events(turn, request, cache_read, cache_creation)
        ):
            if budget is not None and emitted >= budget:
                return  # 截断：不发 completed，模拟连接半途断开
            self._cancel.raise_if_cancelled()
            yield ev

    def _compose_events(
        self, turn: SimTurn, request: ApiRequest, cache_read: int, cache_creation: int
    ) -> Iterator[ResponseEvent]:
        """按服务端事件骨架顺序生成本 turn 的完整事件序列（同步生成器）。"""
        yield created()
        yield server_model(self._model)
        # text：8 字符切片模拟流式
        text = turn.text
        for i in range(0, len(text), _TEXT_CHUNK):
            yield text_delta(text[i : i + _TEXT_CHUNK])
        # tool_call：默认 delta 分片（首片带 name）+ done 收尾；可关闭退化为一次性 done
        malformed = turn.fault is not None and turn.fault.kind == "malformed_arguments"
        for tc in turn.tool_calls:
            call_id = str(tc.get("id", ""))
            name = str(tc.get("name", ""))
            arguments = _MALFORMED_ARGS if malformed else str(tc.get("arguments", "{}"))
            if self._client.chunked_tool_calls:
                for j in range(0, len(arguments), _ARGS_CHUNK):
                    yield tool_call_delta(
                        call_id=call_id,
                        name=name if j == 0 else None,
                        delta=arguments[j : j + _ARGS_CHUNK],
                    )
            yield tool_call_done(call_id=call_id, name=name, arguments=arguments)
        # structured_output：仅当请求要求强类型输出且剧本配了回放数据
        if request.response_format is not None and turn.structured is not None:
            yield structured_output(
                parsed=turn.structured,
                raw_text=json.dumps(turn.structured, ensure_ascii=False),
            )
        # prompt_cache：账本自动折算（或剧本覆写），每 turn 必发——cache 行为全程可观测
        yield prompt_cache(cache_read=cache_read, cache_creation=cache_creation)
        # content_filter finish：镜像真实 provider 行为（抛错而非伪造成功空回复）
        if turn.finish == "content_filter":
            raise ContentFilterError("sim: response blocked by content filter")
        # 并发编排信号在终态前点亮
        if turn.emit_signal is not None:
            self._client.coordinator.signal(turn.emit_signal)
        yield completed(
            response_id="sim-resp",
            usage=turn.usage,
            end_turn=_end_turn(turn),
            request_id=turn.request_id,
        )


def _end_turn(turn: SimTurn) -> bool:
    """finish 语义 → completed.end_turn 映射；未声明沿用「无工具调用即 end_turn」。"""
    if turn.finish is None:
        return not bool(turn.tool_calls)
    return turn.finish in ("end_turn", "length")


class _SimClientBase:
    """SimClient / RoutingSimClient 公共骨架：状态件 + session 工厂。"""

    def __init__(
        self,
        *,
        model: str = "sim-model",
        context_window: int | None = None,
        chunked_tool_calls: bool = True,
        coordinator: SimCoordinator | None = None,
    ) -> None:
        self._model = model
        self.ledger = RequestLedger()
        self.server = SimServerState(context_window=context_window)
        self.validator = RequestContractValidator()
        self.coordinator = coordinator if coordinator is not None else SimCoordinator()
        self.chunked_tool_calls = chunked_tool_calls

    def session(self, *, cancel: CancellationToken, model: str | None = None) -> _SimSession:
        """创建 turn 级 session（脚本选取延迟到 stream 拿到请求时）。"""
        return _SimSession(self, cancel, model or self._model)

    def _next_turn(self, request: ApiRequest) -> SimTurn:
        """由子类实现的脚本选取策略。"""
        raise NotImplementedError

    def _reset_state(self) -> None:
        """复位公共状态件（ledger / 账本 / 信号）。"""
        self.ledger.reset()
        self.server.reset()
        self.coordinator.reset()


class SimClient(_SimClientBase):
    """顺序回放的 conformance 模拟器（旧 ``MockClient`` 的替代者）。

    示例：
        >>> client = SimClient(turns=[
        ...     SimTurn(text="先调用工具", tool_calls=[
        ...         {"id": "c1", "name": "read_skill", "arguments": '{"skill_id": "x"}'},
        ...     ]),
        ...     SimTurn(text="工具返回后的最终回复。"),
        ... ])
    """

    def __init__(self, *, turns: list[SimTurn], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._turns = list(turns)
        self._idx = 0

    def _next_turn(self, request: ApiRequest) -> SimTurn:
        if self._idx >= len(self._turns):
            raise SimScriptExhausted(
                f"脚本已耗尽（共 {len(self._turns)} 个 turn），第 {self._idx + 1} 次采样无剧本可放"
            )
        turn = self._turns[self._idx]
        self._idx += 1
        return turn

    def reset(self) -> None:
        """复位游标与全部状态件（脚本列表不变）。"""
        self._idx = 0
        self._reset_state()


class RoutingSimClient(_SimClientBase):
    """按请求文本标记路由的 conformance 模拟器（旧 ``RoutingMockClient`` 的替代者）。

    ``routes``: ``{marker_substring: [SimTurn, ...]}``。stream 时把请求规范化全文
    （system_prompt + messages，skill body 在 system_prompt 内）按 routes 插入顺序
    找首个命中标记，取其脚本列表推进（每标记独立游标）。并发子 turn 的回放与
    session 创建顺序无关。无标记命中抛 ``KeyError``（禁 silent fallback）；
    游标越界抛 ``SimScriptExhausted``（替代旧静默空 turn）。
    """

    def __init__(self, *, routes: dict[str, list[SimTurn]], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._routes = {k: list(v) for k, v in routes.items()}
        self._cursors: dict[str, int] = dict.fromkeys(routes, 0)

    def _next_turn(self, request: ApiRequest) -> SimTurn:
        # 关键：skill body 进 ApiRequest.system_prompt 而非 messages，
        # 规范化全文两处都覆盖（与 RequestLedger.blob 同基底）
        blob = _canonical_text(request)
        for marker, turns in self._routes.items():
            if marker in blob:
                idx = self._cursors[marker]
                if idx >= len(turns):
                    raise SimScriptExhausted(
                        f"路由 {marker!r} 的脚本已耗尽（共 {len(turns)} 个 turn），"
                        f"第 {idx + 1} 次命中无剧本可放"
                    )
                self._cursors[marker] = idx + 1
                return turns[idx]
        raise KeyError(f"RoutingSimClient: 无标记命中, blob={blob[:120]!r}")

    def reset(self) -> None:
        """复位全部路由游标与状态件（routes 不变）。"""
        self._cursors = dict.fromkeys(self._routes, 0)
        self._reset_state()
