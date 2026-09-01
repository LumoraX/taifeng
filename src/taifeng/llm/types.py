"""LLM 客户端通用类型 —— ApiRequest / TokenUsage / RateLimits / ToolSpecRef。

参照：
    - codex codex-rs/codex-api/src/common.rs
    - claw-code crates/runtime/src/usage.rs
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
# Provider-neutral 输入部件
# ---------------------------------------------------------------------------

type ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
type ImageDetail = Literal["auto", "low", "high", "original"]


class _FrozenPart(BaseModel):
    """不可变、禁止扩展字段的 provider-neutral 输入部件基类。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TextPart(_FrozenPart):
    """多模态消息中的一段文本。"""

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class ImagePart(_FrozenPart):
    """canonical base64 图片；wire 层才临时构造 Data URL。"""

    type: Literal["image"] = "image"
    media_type: ImageMediaType
    base64_data: str = Field(min_length=1)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detail: ImageDetail = "auto"

    @field_validator("base64_data")
    @classmethod
    def _reject_data_url(cls, value: str) -> str:
        """禁止把 provider wire 形态渗入核心契约。"""
        if value.startswith("data:"):
            raise ValueError("image part must contain canonical base64, not a Data URL")
        return value


class ProviderStateEnvelope(_FrozenPart):
    """可恢复但不可解释的 provider 专属状态。"""

    provider: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    item_type: str = Field(min_length=1)
    payload: dict[str, Any]


type PartContent = str | list[TextPart | ImagePart]


class ApiMessageItem(_FrozenPart):
    """按请求顺序保存的语义消息输入项。"""

    type: Literal["message"] = "message"
    role: Literal["system", "user", "assistant"]
    content: PartContent
    sample_id: str | None = None
    output_index: int | None = Field(default=None, ge=0)


class ApiFunctionCallItem(_FrozenPart):
    """按 provider 输出顺序重放的函数调用。"""

    type: Literal["function_call"] = "function_call"
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: str
    sample_id: str = Field(min_length=1)
    output_index: int = Field(ge=0)


class ApiFunctionCallOutputItem(_FrozenPart):
    """与函数调用配对的工具结果。

    ``output`` 是 ``PartContent``：纯文本工具保持 ``str``（wire 逐位不变），
    带图工具为 ``[TextPart, ImagePart, ...]``。Responses 协议原生接受这两种
    形态——参照 codex ``FunctionCallOutputBody`` 的 untagged ``Text |
    ContentItems``，差异 Y 见 ``ToolResult.attachments`` 的说明。
    """

    type: Literal["function_call_output"] = "function_call_output"
    call_id: str = Field(min_length=1)
    output: PartContent
    origin_sample_id: str = Field(min_length=1)


class ApiProviderStateItem(_FrozenPart):
    """保持在请求顺序中的不透明 provider-state item。"""

    type: Literal["provider_state"] = "provider_state"
    sample_id: str = Field(min_length=1)
    output_index: int = Field(ge=0)
    state: ProviderStateEnvelope


type ApiInputItem = (
    ApiMessageItem | ApiFunctionCallItem | ApiFunctionCallOutputItem | ApiProviderStateItem
)


# ---------------------------------------------------------------------------
# API 请求体
# ---------------------------------------------------------------------------


class ApiMessage(BaseModel):
    """跨 provider 统一对话消息。

    与 ``conversation.models.ResponseItem`` 的 wire-level 形态对齐，
    但去掉持久化元数据（id / created_at / metadata）。
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: PartContent
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    reasoning: str | None = None
    """thinking 模型的 reasoning 文本回传载体(provider-neutral 命名)。

    openai_compat / litellm 组装请求体时翻译为 wire 字段 ``reasoning_content``,
    None 时不写键(非 thinking provider 零影响)。见 reasoning-content-passback。
    """


def messages_to_input_items(messages: list[ApiMessage]) -> list[ApiInputItem]:
    """把兼容 messages view 确定性投影成规范有序 input items。"""
    out: list[ApiInputItem] = []
    for message_index, message in enumerate(messages):
        legacy_sample_id = f"legacy:message:{message_index}"
        if message.role == "tool":
            out.append(
                ApiFunctionCallOutputItem(
                    call_id=message.tool_call_id or legacy_sample_id,
                    # 直接透传 PartContent：早先这里对非 str 兜底成 ""，会把工具
                    # 返回的图片连同文本一起静默吞掉（无声的数据丢失）。
                    output=message.content,
                    origin_sample_id=legacy_sample_id,
                )
            )
            continue
        out.append(
            ApiMessageItem(
                role=message.role,
                content=message.content,
                sample_id=legacy_sample_id if message.role == "assistant" else None,
                output_index=message_index if message.role == "assistant" else None,
            )
        )
        for call_index, tool_call in enumerate(message.tool_calls or []):
            function = tool_call.get("function", {})
            out.append(
                ApiFunctionCallItem(
                    call_id=str(tool_call.get("id", f"{legacy_sample_id}:call:{call_index}")),
                    name=str(function.get("name", "")),
                    arguments=str(function.get("arguments", "{}")),
                    sample_id=legacy_sample_id,
                    output_index=message_index + call_index,
                )
            )
    return out


def input_items_to_messages(items: list[ApiInputItem]) -> list[ApiMessage]:
    """把规范 items 投影为旧 provider 可用的语义 messages view。"""
    messages: list[ApiMessage] = []
    latest_assistant: ApiMessage | None = None
    for item in items:
        if isinstance(item, ApiMessageItem):
            messages.append(ApiMessage(role=item.role, content=item.content))
            latest_assistant = messages[-1] if item.role == "assistant" else None
        elif isinstance(item, ApiFunctionCallItem):
            if latest_assistant is None:
                latest_assistant = ApiMessage(role="assistant", content="")
                messages.append(latest_assistant)
            tool_call = {
                "id": item.call_id,
                "type": "function",
                "function": {"name": item.name, "arguments": item.arguments},
            }
            latest_assistant.tool_calls = [*(latest_assistant.tool_calls or []), tool_call]
        elif isinstance(item, ApiFunctionCallOutputItem):
            messages.append(ApiMessage(role="tool", content=item.output, tool_call_id=item.call_id))
            latest_assistant = None
        # provider state 必须保留在 canonical item 流，不会被渲染成自然语言。
    return messages


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
        - Sim: 与 ``SimTurn.structured`` 字段配对回放
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
    messages: list[ApiMessage] = Field(default_factory=list)
    input_items: list[ApiInputItem] = Field(default_factory=list)
    tools: list[ToolSpecRef] = Field(default_factory=list)
    parallel_tool_calls: bool = True
    max_output_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    cache_breakpoints: list[CacheBreakpoint] = Field(default_factory=list)
    response_format: ResponseFormatSpec | None = None
    """强类型输出 schema；非 None 时 provider 会强制 LLM 返回符合 schema 的 JSON。"""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _synchronize_input_views(self) -> ApiRequest:
        """保持旧 messages view 与规范 input_items 的确定性一致。"""
        messages_supplied = "messages" in self.model_fields_set
        items_supplied = "input_items" in self.model_fields_set
        if items_supplied and messages_supplied:
            derived = input_items_to_messages(self.input_items)
            if derived != self.messages:
                raise ValueError("messages and input_items disagree")
        elif items_supplied:
            self.messages = input_items_to_messages(self.input_items)
        else:
            self.input_items = messages_to_input_items(self.messages)
        return self
