"""OpenAI 官方 ``/v1/chat/completions`` 客户端。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import UnsupportedCombinationError
from taifeng.llm.providers.openai._shared import (
    OPENAI_DEFAULT_BASE_URL,
    build_openai_headers,
    chat_content,
    reject_provider_state,
)
from taifeng.llm.providers.openai_compat import OpenAICompatClient, OpenAICompatSession

if TYPE_CHECKING:
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken


class OpenAIChatSession(OpenAICompatSession):
    """单次官方 Chat turn；复用稳定的 Chat SSE 归一化实现。"""

    def _build_payload(self, req: ApiRequest) -> dict[str, Any]:
        """构造官方 Chat payload，并在序列化前完成协议 preflight。"""
        reject_provider_state(req.input_items, protocol="Chat")
        model = req.model or self._model
        if (
            model.lower().startswith("gpt-5.6")
            and req.tools
            and req.reasoning_effort not in (None, "none")
        ):
            raise UnsupportedCombinationError(
                "GPT-5.6 Chat with tools requires reasoning_effort='none' or unset"
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt}
            for prompt in req.system_prompt
            if prompt
        ]
        for message in req.messages:
            wire: dict[str, Any] = {
                "role": message.role,
                "content": chat_content(message.content),
            }
            if message.tool_call_id is not None:
                wire["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                wire["tool_calls"] = message.tool_calls
            if message.reasoning is not None:
                wire["reasoning_content"] = message.reasoning
            messages.append(wire)

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "store": False,
        }
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_output_tokens is not None:
            payload["max_completion_tokens"] = req.max_output_tokens
        if req.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in req.tools
            ]
            payload["parallel_tool_calls"] = req.parallel_tool_calls
        if req.reasoning_effort is not None:
            payload["reasoning_effort"] = req.reasoning_effort
        if req.response_format is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": req.response_format.name,
                    "schema": req.response_format.json_schema,
                    "strict": req.response_format.strict,
                },
            }
        return payload


class OpenAIChatClient(OpenAICompatClient):
    """OpenAI 官方 Chat 协议客户端，显式支持文字与图片输入。"""

    capabilities = ModelCapabilities(
        input_modalities=frozenset({"text", "image"}),
        provider="openai",
        protocol="chat",
    )

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5.6",
        base_url: str = OPENAI_DEFAULT_BASE_URL,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            extra_headers=extra_headers,
            timeout_seconds=timeout_seconds,
        )

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> OpenAIChatSession:
        """创建无 IO 的官方 Chat session。"""
        session = OpenAIChatSession(
            base_url=self._base_url,
            api_key=self._api_key,
            model=model or self._default_model,
            cancel=cancel,
            extra_headers=self._extra_headers,
            timeout_seconds=self._timeout_seconds,
            previous_cache_read=self._previous_cache_read,
        )
        session._headers = build_openai_headers(self._api_key, self._extra_headers)
        return session
