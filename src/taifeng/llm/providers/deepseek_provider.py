"""DeepSeek 原生 provider —— ``OpenAICompatClient`` 薄子类。

DeepSeek API 与 OpenAI chat/completions **100% 兼容**（payload / SSE /
tool_calls 协议一致），仅 usage 字段命名不同（``prompt_cache_hit_tokens`` /
``prompt_cache_miss_tokens`` vs OpenAI 标准 ``prompt_tokens_details.cached_tokens``）。
该差异已在 ``_shared.extract_usage_openai_family`` 的三优先级查找内统一处理，
DeepSeek 无需重写任何 session 逻辑。

模型：
    - ``deepseek-chat`` —— V3 通用对话
    - ``deepseek-reasoner`` —— R1 推理；流式 ``reasoning_content`` delta 已被
      ``OpenAICompatSession._process_chunk`` 处理（emit ``reasoning_delta``）

构造默认值预设：``base_url="https://api.deepseek.com"``、
``model="deepseek-chat"``。
"""

from __future__ import annotations

from taifeng.llm.providers.openai_compat import OpenAICompatClient


class DeepSeekClient(OpenAICompatClient):
    """DeepSeek native client —— OpenAI-compat 子类，预设官方 endpoint。

    用法：
        >>> client = DeepSeekClient(api_key="sk-xxx")  # V3
        >>> client = DeepSeekClient(api_key="sk-xxx", model="deepseek-reasoner")  # R1

    与裸的 ``OpenAICompatClient(base_url="https://api.deepseek.com", ...)``
    等价，但提供：
        - pinned 默认 ``base_url`` / ``model``（避免每次手填）
        - 选型表入口（文档与读者通过 ``DeepSeekClient`` 找到该路径）
        - cache 字段精准映射（共享 ``_shared.extract_usage_openai_family``，
          自动识别 ``prompt_cache_hit_tokens``）
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            extra_headers=extra_headers,
            timeout_seconds=timeout_seconds,
        )


__all__ = ["DeepSeekClient"]
