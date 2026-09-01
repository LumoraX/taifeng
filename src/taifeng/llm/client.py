"""ModelClient 协议 —— 跨 provider 统一抽象。

参照：codex codex-rs/core/src/client.rs::ModelClient
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken


@dataclass(frozen=True)
class ModelCapabilities:
    """Model client 输入协议能力的只读描述。"""

    input_modalities: frozenset[Literal["text", "image"]]
    provider: str
    protocol: str
    accepts_provider_state: bool = False
    tool_output_modalities: frozenset[Literal["text", "image"]] = frozenset({"text"})
    """``function_call_output`` 能承载的模态。

    与 ``input_modalities`` **分开**声明：OpenAI Chat 的 user 消息能带图，但
    tool 消息的 content 只能是字符串，两者能力不同；合并声明必然误判。

    默认 text-only —— 与 ``input_modalities`` 同规矩：能力一律显式打开，
    **不得**据模型名或域名自动推断。
    """


TEXT_ONLY_CAPABILITIES = ModelCapabilities(
    input_modalities=frozenset({"text"}),
    provider="unknown",
    protocol="unknown",
)


def model_capabilities(client: object) -> ModelCapabilities:
    """读取 client 能力；未迁移的旧实现安全降级为 text-only。"""
    value = getattr(client, "capabilities", None)
    if isinstance(value, ModelCapabilities):
        return value
    return TEXT_ONLY_CAPABILITIES


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

    async def __aenter__(self) -> ModelClientSession: ...

    async def __aexit__(self, *exc: object) -> None: ...


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


class OneNetworkAttemptModelClient(ABC):
    """session 构造无 effect，且每次 ``stream`` 恰有一个网络 attempt 的 nominal 边界。

    只有实现 owner 审核过 session/stream 全调用链后才能显式继承。duck typing、
    virtual subclass 或外部 wrapper 均不能声明该能力。
    """

    @abstractmethod
    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> ModelClientSession:
        """创建无 IO/effect 的 session；真实 dispatch 只能发生在 ``stream``。"""
