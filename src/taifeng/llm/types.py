"""LLM 客户端通用类型 —— ApiRequest / TokenUsage / RateLimits / ToolSpecRef。

参照：
    - codex codex-rs/codex-api/src/common.rs
    - claw-code crates/runtime/src/usage.rs
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Token 用量与限速
# ---------------------------------------------------------------------------


class TokenUsage(BaseModel):
    """单次 LLM 调用的 token 计量。

    跨 provider 字段尽量标准化；provider 独有字段塞 ``raw``。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    reasoning_tokens: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def cached_ratio(self) -> float:
        """命中缓存的比例（read / (read + creation + uncached_input)）。"""
        denom = self.input_tokens or 1
        return self.cache_read_input_tokens / denom


class RateLimitSnapshot(BaseModel):
    """provider 返回的速率限制提示。"""

    requests_remaining: int | None = None
    requests_reset_seconds: float | None = None
    tokens_remaining: int | None = None
    tokens_reset_seconds: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool 描述（LLM 视角的工具 schema）
# ---------------------------------------------------------------------------


class ToolSpecRef(BaseModel):
    """LLM 视角的 tool 描述（不含执行逻辑）。

    ``ToolRegistry`` 把 ``ToolSpec`` 转为 ``ToolSpecRef`` 注入 prompt。
    """

    name: str
    description: str
    input_schema: dict[str, Any]


# ---------------------------------------------------------------------------
# API 请求体
# ---------------------------------------------------------------------------


class ApiMessage(BaseModel):
    """跨 provider 统一对话消息。

    与 ``conversation.models.ResponseItem`` 的 wire-level 形态对齐，
    但去掉持久化元数据（id / created_at / metadata）。
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: list[dict[str, Any]] | str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class CacheBreakpoint(BaseModel):
    """显式声明 prompt cache 边界。

    Anthropic 支持 ``cache_control``；其他 provider 暂存字段以备未来。
    ``index`` 指向 messages 列表中该消息（含）之前的位置应被缓存。
    """

    index: int
    ttl_seconds: int = 300


class ResponseFormatSpec(BaseModel):
    """LLM 响应强类型 schema —— provider-neutral 描述。

    业务侧传 JSON Schema dict（可用 ``MyPydanticModel.model_json_schema()`` 生成）。
    Taifeng 保持 provider-neutral：**不绑定** Pydantic / 不内置 schema。

    Provider 翻译规则：
        - OpenAI / openai-compat: ``response_format = {"type": "json_schema",
          "json_schema": {"name": ..., "schema": ..., "strict": ...}}``
        - LiteLLM: 同上（内部自动桥接到 Anthropic / Gemini native 格式）
        - Mock: 与 ``MockTurn.structured`` 字段配对回放
    """

    name: str
    """schema 名（必填，作为 LLM 看到的 tag）"""

    json_schema: dict[str, Any]
    """JSON Schema dict（业务侧可用 BaseModel.model_json_schema() 生成）"""

    strict: bool = True
    """OpenAI strict mode：True 要求所有字段必须出现且无 additionalProperties。"""


class ApiRequest(BaseModel):
    """单次 LLM 调用的统一请求体。

    Provider adapter 负责把它翻译为 OpenAI / Anthropic / Gemini 原生格式。
    """

    model: str
    system_prompt: list[str] = Field(default_factory=list)
    messages: list[ApiMessage]
    tools: list[ToolSpecRef] = Field(default_factory=list)
    parallel_tool_calls: bool = True
    max_output_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    cache_breakpoints: list[CacheBreakpoint] = Field(default_factory=list)
    response_format: ResponseFormatSpec | None = None
    """强类型输出 schema；非 None 时 provider 会强制 LLM 返回符合 schema 的 JSON。"""
    metadata: dict[str, Any] = Field(default_factory=dict)
