"""模型侧预热协议 —— 在用户开口之前把静态前缀送进 provider 的缓存（prewarm，ADR 0092）。

会话的第一次采样最慢：连接要建，system prompt 与工具清单这段静态前缀要被 provider 首次处理并
写入 prompt cache。预热把这部分开销挪到用户输入之前。

内核只定协议，不假定任何 provider 的做法（ADR 0017 规则③）：

- ``ModelPrewarmer``：拿到「下一次采样会发出的请求」，自行决定怎么预热；
- ``CachePrimingPrewarmer``：参考实现，用一次输出极短的采样把前缀写进缓存。它**会消耗 token**，
  是否值得由业务按自己的 provider 计费与缓存时效判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from taifeng.llm.types import ApiMessageItem, ApiRequest, TokenUsage

if TYPE_CHECKING:
    from taifeng.llm.client import ModelClient
    from taifeng.llm.events import ResponseEvent
    from taifeng.loop.cancellation import CancellationToken


@dataclass(frozen=True)
class PrewarmOutcome:
    """一次模型侧预热的结果。

    Attributes:
        primed: 是否真的做了预热；False = 该实现在当前条件下无事可做。
        usage: 预热消耗的 token；没有消耗为 None。
        detail: 给运维看的补充说明。
    """

    primed: bool
    usage: TokenUsage | None = None
    detail: str = ""


@runtime_checkable
class ModelPrewarmer(Protocol):
    """模型侧预热协议。"""

    async def prewarm(
        self, request: ApiRequest, *, cancel: CancellationToken,
    ) -> PrewarmOutcome:
        """预热。

        Args:
            request: 下一次采样会发出的请求（system prompt、工具清单、已有的 history），
                不含尚未到来的用户输入。实现 MUST NOT 修改它。
            cancel: 取消 token；实现 MUST 可取消（R4）——用户输入到达时预热会被取消。

        Raises:
            Exception: 预热失败。内核记录后照常继续，失败不影响之后的 turn。
        """
        ...


class CachePrimingPrewarmer:
    """用一次输出极短的采样把静态前缀写进 provider 的 prompt cache。

    在请求末尾追加一条探针用户消息（前缀不变，缓存键不受影响），把输出上限压到最小，
    丢弃模型的回答。history 为空的新会话与已有 history 的会话都适用。
    """

    def __init__(
        self,
        model_client: ModelClient,
        *,
        probe_text: str = "ping",
        max_output_tokens: int = 1,
    ) -> None:
        """
        Args:
            model_client: 发探针用的客户端；通常就是会话用的那个。
            probe_text: 探针用户消息的正文。
            max_output_tokens: 探针采样的输出上限。

        Raises:
            ValueError: ``probe_text`` 为空，或 ``max_output_tokens`` 小于 1。
        """
        if not probe_text.strip():
            raise ValueError("probe_text must not be empty")
        if max_output_tokens < 1:
            raise ValueError(f"max_output_tokens must be at least 1, got {max_output_tokens!r}")
        self._client = model_client
        self._probe_text = probe_text
        self._max_output_tokens = max_output_tokens

    async def prewarm(
        self, request: ApiRequest, *, cancel: CancellationToken,
    ) -> PrewarmOutcome:
        """发一次探针采样并读完它的事件流。"""
        cancel.raise_if_cancelled()
        probe = self._probe(request)
        usage: TokenUsage | None = None
        async with self._client.session(cancel=cancel, model=request.model or None) as session:
            async for event in session.stream(probe):
                cancel.raise_if_cancelled()
                if event.kind == "error":
                    raise RuntimeError(f"prewarm probe failed: {_error_text(event)}")
                if event.kind == "completed":
                    usage = _usage_of(event)
        return PrewarmOutcome(primed=True, usage=usage, detail="cache priming probe")

    def _probe(self, request: ApiRequest) -> ApiRequest:
        """在原请求之后追加探针消息、压低输出上限；原请求不被修改。"""
        fields = request.model_dump(exclude={"messages", "input_items"})
        fields["max_output_tokens"] = self._max_output_tokens
        # 探针不需要结构化输出；工具清单保留——它是缓存前缀的一部分
        fields["response_format"] = None
        return ApiRequest(
            **fields,
            # 只给规范输入项，messages 由它派生（provider 状态等非消息项原样保留）
            input_items=[
                *request.input_items,
                ApiMessageItem(role="user", content=self._probe_text),
            ],
        )


def _usage_of(event: ResponseEvent) -> TokenUsage | None:
    """从 ``completed`` 事件里取 usage；事件不带 usage 时为 None。"""
    usage = event.data.get("usage")
    return TokenUsage.model_validate(usage) if isinstance(usage, dict) else None


def _error_text(event: ResponseEvent) -> str:
    """从 ``error`` 事件里取可读的错误说明。"""
    for name in ("message", "error", "kind"):
        value = event.data.get(name)
        if value:
            return str(value)
    return "unknown error"


__all__ = ["CachePrimingPrewarmer", "ModelPrewarmer", "PrewarmOutcome"]
