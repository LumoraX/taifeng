"""ModelClient 协议 —— 跨 provider 统一抽象。

参照：codex codex-rs/core/src/client.rs::ModelClient
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from taifeng.llm.events import ResponseEvent
from taifeng.llm.types import ApiRequest
from taifeng.loop.cancellation import CancellationToken


@runtime_checkable
class ModelClientSession(Protocol):
    """Turn 级 session。每个 turn 重建，避免 sticky header / cache key 跨 turn 污染。"""

    def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """流式调用，按 ``ResponseEvent`` 顺序产出。

        实现要点：
            - 必须支持 cancel：检测到 cancel 时关闭底层连接并抛 CancelledError
            - 必须确保最后一个事件是 ``completed`` 或 ``error``（不允许静默停止）
        """
        ...

    async def __aenter__(self) -> ModelClientSession:
        ...

    async def __aexit__(self, *exc: object) -> None:
        ...


@runtime_checkable
class ModelClient(Protocol):
    """Session 级客户端。

    保留 provider auth、cache 统计、重试配置；跨 turn 复用。
    """

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        """创建一个 turn 级 session。"""
        ...
