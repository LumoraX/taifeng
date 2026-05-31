"""LLM Provider 适配 —— native 四件套 + LiteLLM 兜底 + Mock 测试。

选型：
    - OpenAI / 自部署 OpenAI-compat (vLLM, Ollama, one-api) → ``OpenAICompatClient``
    - Anthropic Claude（含 cache_control / messages 流式） → ``AnthropicClient``
    - Google Gemini（AI Studio） → ``GeminiClient``
    - DeepSeek（V3 / R1，含 prompt cache） → ``DeepSeekClient``
    - AWS Bedrock / GCP Vertex / Azure / 其他自定义 endpoint → ``LiteLLMClient``

设计文档：docs/architecture/overview.md §LLM Provider
"""

from taifeng.llm.providers.anthropic_provider import (
    AnthropicClient,
    AnthropicSession,
)
from taifeng.llm.providers.deepseek_provider import DeepSeekClient
from taifeng.llm.providers.gemini_provider import GeminiClient, GeminiSession
from taifeng.llm.providers.litellm_provider import LiteLLMClient, LiteLLMSession
from taifeng.llm.providers.mock import MockClient, MockSession, MockTurn
from taifeng.llm.providers.openai_compat import (
    OpenAICompatClient,
    OpenAICompatSession,
)

__all__ = [
    "AnthropicClient",
    "AnthropicSession",
    "DeepSeekClient",
    "GeminiClient",
    "GeminiSession",
    "LiteLLMClient",
    "LiteLLMSession",
    "MockClient",
    "MockSession",
    "MockTurn",
    "OpenAICompatClient",
    "OpenAICompatSession",
]
