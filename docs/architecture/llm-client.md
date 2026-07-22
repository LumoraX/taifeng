# LLM 客户端

> §1.5 —— `ModelClient` session 抽象、`ResponseEvent` enum、多 provider 适配、重试。

## 设计目标

- 一套 `ModelClient` 抽象兼容 OpenAI / Anthropic / Gemini / OpenAI-compat 本地模型
- `ResponseEvent` enum 标准化流式事件（参照 codex 范式，实现 11 类 EventKind）
- 重试 / 失败转移 / cache 统计在客户端层封装
- session 级 vs turn 级两层 client：避免 sticky header 跨 turn 污染

参照：
- `codex` `ModelClient` (session) + `ModelClientSession` (turn) + `ResponseEvent` enum
- `claw-code` `crates/api/client.rs` + `prompt_cache.rs` + `sse.rs`
- `LiteLLM` —— Python 多 provider 适配最成熟方案（可作为底层）

## 核心抽象

```python
# src/taifeng/llm/events.py

from typing import Literal
from pydantic import BaseModel

EventKind = Literal[
    "created",                # 流开始
    "text_delta",             # 文本增量
    "tool_call_delta",        # 工具调用参数 streaming
    "tool_call_done",         # 工具调用完整体
    "reasoning_delta",        # Claude thinking / o1 reasoning
    "rate_limits",            # 提供商速率限制窗口（RateLimitSnapshot）
    "server_model",           # 提供商实际使用的模型（可能与请求不同）
    "prompt_cache",           # cache 命中 / 创建 token 元数据
    "structured_output",      # response_format 命中时的结构化 JSON
    "completed",              # 流正常结束
    "error",                  # 流错误
]

class ResponseEvent(BaseModel):
    kind: EventKind           # 11 类
    data: dict[str, Any]
```

```python
# src/taifeng/llm/client.py

from typing import Protocol, AsyncIterator

class ModelClient(Protocol):
    """Session 级客户端。

    保留 provider auth、cache 统计、重试配置。
    跨 turn 复用。
    """

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> "ModelClientSession":
        """创建一个 turn 级 session（同步工厂；返回的 session 是 async context manager）。"""


class ModelClientSession(Protocol):
    """Turn 级 session。

    每个 turn 重建，避免 sticky header / cache key 跨 turn 污染。
    """

    def stream(
        self,
        request: "ApiRequest",
    ) -> AsyncIterator[ResponseEvent]:
        """流式调用，按 ResponseEvent 产出。"""

    async def __aenter__(self) -> "ModelClientSession": ...
    async def __aexit__(self, *exc) -> None: ...
```

`stream()` 在协议层是返回 `AsyncIterator` 的普通方法；具体 provider 使用带
`yield` 的 `async def` 实现异步生成器。调用方直接 `async for event in
session.stream(request)`，不先 `await stream()`。具体 provider 的 session 类型只要
结构化满足 `ModelClientSession`，其 `session()` 就可协变返回该具体类型，无需给
`ModelClient` 增加泛型参数或运行时 cast。

## ApiRequest 统一格式

```python
# src/taifeng/llm/types.py

class ApiRequest(BaseModel):
    """跨 provider 统一请求格式。"""
    model: str
    system_prompt: list[str] = Field(default_factory=list)  # 多段 system；provider 内部合并
    messages: list[ApiMessage]                              # 注：ApiMessage（已转 API 形态），非 ResponseItem
    tools: list[ToolSpecRef] = Field(default_factory=list)  # 注：ToolSpecRef（轻量引用），非完整 ToolSpec
    parallel_tool_calls: bool = True
    max_output_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    cache_breakpoints: list[CacheBreakpoint] = Field(default_factory=list)  # 显式 cache 边界
    response_format: ResponseFormatSpec | None = None       # 强类型输出 schema（structured output）
    metadata: dict[str, Any] = Field(default_factory=dict)  # provider-specific 透传
```

`cache_breakpoints` 是关键 —— 告诉 provider「这些位置之前的内容应该被缓存」。Anthropic 支持显式 `cache_control`，OpenAI 暂不支持但保留字段以备未来。**坐标系契约**：`CacheBreakpoint.index` 是 **messages 下标**；`build_api_request` 负责把压缩返回的 `cache_anchor_index`（history 下标，`[0, N)` 稳定前缀）换算到 messages 坐标——打点在稳定前缀产出的最后一条消息，前缀无产出消息则不打点（history→messages 非 1:1：记账 item 跳过、同轮合并折叠）。`response_format` 非 None 时 provider 强制 LLM 返回符合 schema 的 JSON（详见 `capabilities/llm-structured-output.md`）。

### reasoning 回传（thinking 模型续传契约）

`ApiMessage` 带可选字段 `reasoning: str | None`（provider-neutral 命名）。thinking 模型（deepseek-v4/r 系等）要求带 tool_calls 的 assistant 消息在续传时回传 `reasoning_content`，否则 400 拒：

- **来源**：采样期 `reasoning_delta` 累积落史为 `kind="reasoning"` ResponseItem（紧邻配对 assistant message 之前）；prompt 重建（`loop/prompt.py::history_to_api_messages`）做**同轮合并**——一次采样的 assistant 文本 + 全部 tool_calls 归并回一条 assistant ApiMessage，reasoning 附在其上（thinking 模型校验**每条**带 tool_calls 的 assistant 消息，拆多条形态无法满足）。
- **组装**：openai_compat / litellm 仅在 `reasoning` 非 None 时写 wire 字段 `reasoning_content`。回传天然自限：history 无 reasoning item 即不回传，非 thinking 模型零变化。
- **旋钮**：`reasoning_passback: bool = True`（Engine / Pool 构造参数），仅控回传；落史无条件（R5）。

## Provider 适配

**双层架构：native 四件套（直连 HTTP，零 SDK）+ LiteLLM 兜底**。

```
src/taifeng/llm/providers/
├── openai_compat.py        # OpenAI / vLLM / Ollama / one-api 等 OpenAI-compat gateway（含 reasoning_content）
├── anthropic_provider.py   # Anthropic messages API（cache_control / extended thinking，零 anthropic-sdk）
├── gemini_provider.py      # Gemini streamGenerateContent（零 google-genai-sdk）
├── deepseek_provider.py    # DeepSeek（openai_compat 薄子类，预设 base_url + prompt_cache_hit_tokens 映射）
├── litellm_provider.py     # 兜底：Bedrock / Vertex / Azure / Kimi 等非主流 provider
├── sim/                    # conformance 模拟器 SimClient / RoutingSimClient（测试用，CI 禁真实 API；契约见 capabilities/llm-sim-conformance.md）
└── _shared.py              # classify_http_error（按 status code）/ SSE 解析 / usage 统一
```

四家 native + LiteLLM 共享统一 `ModelClient` 协议 + `ResponseEvent` 流形状
（`created → server_model → text_delta* → tool_call_done* → prompt_cache → completed`）。
异常终止（如 `finish_reason=content_filter`）以 `error` 事件 + 抛 `LLMError` 收尾，**不**伪造 `completed`
（见下「异常终止与空回复保护」）。错误分类走 `_shared.py::classify_http_error`（基于 HTTP status code，比 LiteLLM 的 message 关键字匹配精准）。

业务侧通过依赖注入选择：
```python
from taifeng.llm.providers.anthropic_provider import AnthropicClient

engine = AgentEngine(
    model_client=AnthropicClient(default_model="claude-sonnet-4-6", api_key=...),
    ...
)
```

## 重试与失败转移

```python
# src/taifeng/llm/retry.py
# 注：@dataclass，非 BaseModel

@dataclass
class RetryConfig:
    max_attempts: int = 3
    min_delay_ms: int = 500
    max_delay_ms: int = 30_000
    backoff_multiplier: float = 2.0
    jitter: float = 0.2                    # ±20% 抖动
    retryable_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset({"rate_limit", "transient_network", "server_error"})
    )

async def retry_async(func, config, cancel) -> Any:
    """指数退避（min_delay * multiplier**attempt，capped at max_delay）+ jitter + 服务端 hint delay。
    异常的 ``kind`` 不在 retryable_kinds 中则立即透传，不重试。"""
```

`retryable_kinds` 匹配 `LLMError.kind`（见 `llm/errors.py`）。**不重试的错误**：
- `4xx` 客户端错误（除 429）—— 是 bug，重试无用
- `content_filter` —— provider 拒绝，重试是浪费
- `context_overflow` —— 必须先压缩，重试是死循环
- `cancelled` —— 取消是用户意图

### 错误分类与恢复（G3）

`llm/errors.py` 把异常分类到 `FailureClass`（11 桶：`context_window` / `provider_auth` / `provider_rate_limit` /
`provider_transport` / `provider_internal` / `invalid_request` / `content_filter` / `cancelled` /
`request_size` / `runtime_io` / `unknown`），每类带 `suggested_action`。`llm/recovery.py::recommend_recovery(failure_class)`
产出机读 `RecoveryPlan{steps, auto_retry_once, escalate}`，随 `TurnFailed.data['recovery']` 透出——**内核只产出建议，业务侧编排执行**（R1）。
native 三家成功时还会 emit `rate_limits`（`RateLimitSnapshot`）+ 回填 `LLMError.request_id`（服务端 request-id）。

### 异常终止与空回复（finish_reason）

**判据：只有 LLM「显式报错」才是错误；模型「没产出内容」本身不是错误。** 二者分别处理：

1. **显式错误 → 暴露（Provider 层 finish_reason）**：`openai_compat` 在流末读 `choices[].finish_reason`。当
   `finish_reason=content_filter`（模型/网关安全策略主动拦截，返回空 content + 0 token）时，**不再丢弃该信号**，
   而是先 emit `error` 事件再抛 `ContentFilterError`（与 HTTP 错误路径一致）→ turn `success=False` →
   `call_skill` 回 `ToolResult.error(is_error=true)`。旧实现完全忽略 finish_reason，把「被拦截」伪造成
   「成功的空 completed」——真实事故根因（Gemini 经网关对部分子 skill prompt 误杀，且**非确定性**）。
2. **无显式错误的空 → 容忍继续（Loop 层不臆断）**：`loop/turn.py` 对「无 text + 无 tool call」的终止轮按
   **正常完成**处理（`success=True`、`final_text=""`），`call_skill` 回 `ToolResult.ok("")`，父 turn 拿到
   空结果继续即可。内核**不**把「空」臆断成异常——空可能源于 prompt / skill，归因交业务侧（避免把上游/业务
   问题误甩锅给 LLM）。

> 决策记录：曾尝试在 loop 层加「空即异常」守卫（`EmptyCompletionError`），后按评审改为上述判据——
> 空非错误、继续即可，只有 LLM 显式信号（finish_reason / error / 非 200）才判错。

## Sticky 路由 & subagent 头

native provider 实现可在请求里带额外 header（如 `extra_headers`），供业务侧 provider gateway
（new-api / portkey 等）做 sticky routing（同 turn 命中同 cache pool）、子 agent 计费区分、父子 thread 追踪。
**这些 header 不是 `ModelClientSession` 协议的一部分**——协议只约定 `stream(request)`；header 由具体 provider
实现或业务侧注入（保持协议层 R1-clean）。

## Cache 统计

cache 统计**不挂在 `ModelClient` 上**，而是 `Engine` 跨 turn 持有一份 `PromptCacheStats`
（见 `context/cache_stats.py`，详见 context-compression.md）。每个 turn 的 `usage`（含
`cache_creation_input_tokens` / `cache_read_input_tokens`）由 provider 经 `prompt_cache` / `completed`
事件透出，`turn.py` 调 `PromptCacheStats.record_turn(...)` 累积并归因 cache break。

## 测试用例（M1 验收）

> 全部已覆盖（`tests/` 下 provider / retry / structured-output 测试 + `tests/loop/test_cache_break_reason.py`）。

- [x] provider 流式调用产出 `ResponseEvent` 序列符合规范（SimClient + native）
- [x] 取消 `cancel.cancel()` 后，stream 在 100ms 内停止；HTTP 连接关闭
- [x] 429 重试遵循 `Retry-After`；超 `max_attempts` 透出 `LLMError`
- [x] `cache_breakpoints` 字段对 Anthropic 正确翻译为 `cache_control`
- [x] cache 统计跨 turn 归因正确（expected vs unexpected break）
