# OpenAI 图片输入与 Chat/Responses 双协议设计

## 背景

Taifeng 当前已经能在 `UserMessage.attachments` 中接收并持久化完整内联 base64
附件，strict audit 路径也会校验 base64、声明大小、SHA-256 与调用方注入的单项/总量
上限。但是 LLM prompt 组装只读取 `user_message.payload.text`，附件不会进入
`ApiRequest`，因此现状只具备“附件存储”，不具备“模型图片输入”。

当前 `OpenAICompatClient` 只实现 OpenAI-compatible
`/v1/chat/completions` wire protocol。OpenAI `/v1/responses` 的输入 Item、工具调用、
流式事件、Structured Outputs 与状态续传语义均不同，不能把 endpoint 字符串替换视为协议支持。

本设计交付第一段真正的多模态输入能力：图片输入。它采用 provider-neutral 内核模型，
并在 `openai` provider 下明确区分 Chat 与 Responses 两套协议。

官方协议依据：

- [Images and vision](https://developers.openai.com/api/docs/guides/images-vision)
- [Migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [Responses API reference](https://developers.openai.com/api/reference/resources/responses)

## 目标

- 支持 PNG、JPEG、WebP 与非动画 GIF 的完整内联 base64 图片输入。
- 支持文字加图片、多图以及纯图片用户消息。
- 在 LLM 层引入 provider-neutral 的 `TextPart` / `ImagePart`，不让 OpenAI Data URL、
  `image_url`、`input_image` 等 wire 字段泄漏到 conversation/context。
- 在 `openai` provider 下提供独立的 `OpenAIChatClient` 与
  `OpenAIResponsesClient`。
- 保留 `OpenAICompatClient` 及其 vLLM、Ollama、LM Studio、one-api、new-api 等兼容
  端点行为。
- Responses 默认 `store=false`，以 Taifeng JSONL 为唯一恢复事实源，支持手工 Item
  重放、工具调用与加密 reasoning 状态续传。
- 图片资源计入请求字节、上下文 token、压缩与 cache anchor 判断。
- 对图片输入、协议转换、流式事件、冷恢复和真实 GPT-5.6 建立可复跑证据。

## 非目标

- 不支持音频、视频、PDF、任意文件输入。
- 不支持图片生成、图片编辑或其他图片输出。
- 不支持图片 URL、临时路径、OpenAI Files API `file_id` 或外置 blob 引用。
- 不为业务自动选择 Chat/Responses，不根据模型名称隐式切换协议。
- 不把 OpenAI hosted tools、Conversations API 或 `previous_response_id` 引入 Taifeng。
- 不承诺所有 OpenAI-compatible 网关均支持多模态；`OpenAICompatClient` 只做既有 Chat
  compatibility 保证。
- 不在第一阶段为 Anthropic、Gemini、DeepSeek、LiteLLM 增加图片 wire 适配。

## 总体方案

### 业务显式启用

协议和图片能力都通过依赖注入显式选择：

```python
client = OpenAIChatClient(...)
client = OpenAIResponsesClient(...)
client = OpenAICompatClient(...)
```

业务同时注入 provider-neutral 的 `ImageInputPolicy`。`enabled=False` 时图片在网络请求
前以稳定 `unsupported_modality` 拒绝；`enabled=True` 时才执行图片 admission、prompt
转换和 provider wire 转换。Taifeng 不维护“按模型名猜协议”的全局开关。

### provider 目录

```text
src/taifeng/llm/providers/
├── openai_compat.py          # 保留现有兼容 Chat 客户端
└── openai/
    ├── __init__.py
    ├── _shared.py            # OpenAI 专用鉴权、HTTP、usage、错误与取消
    ├── chat.py               # /v1/chat/completions
    └── responses.py          # /v1/responses
```

`openai/_shared.py` 复用现有 `providers/_shared.py` 的通用分类器，不复制 HTTP 状态、
rate-limit、request-id 或 usage 提取规则。它只承载 OpenAI 两协议共同但其他 provider
不必继承的行为，例如官方默认 base URL、`store=false` 与 OpenAI SSE frame 读取。

## 数据契约

### canonical 图片附件

对外图片输入使用以下 V1 形状：

```python
class ImageAttachmentV1(BaseModel):
    kind: Literal["image"]
    media_type: Literal[
        "image/png", "image/jpeg", "image/webp", "image/gif"
    ]
    size: int
    sha256: str
    encoding: Literal["base64"] = "base64"
    content: str
    detail: Literal["auto", "low", "high", "original"] = "auto"
```

JSONL 中 `content` 必须是 canonical base64，不得写 Data URL、URL、临时路径或 OpenAI
file id。`size` 表示解码后字节数，`sha256` 是解码后 bytes 的小写十六进制 digest。

现有通用 `AttachmentV1` 继续承担旧记录解码兼容。它增加可选 `detail` 字段，并在
`kind="image"` 时收敛为 `ImageAttachmentV1` 语义；既有非图片附件仍可持久化，但第一阶段
不进入 LLM prompt。旧 JSONL 不含 `detail` 时按 `auto` 读取。

### LLM provider-neutral content

```python
class TextPart(BaseModel):
    type: Literal["text"]
    text: str


class ImagePart(BaseModel):
    type: Literal["image"]
    media_type: Literal[
        "image/png", "image/jpeg", "image/webp", "image/gif"
    ]
    base64_data: str
    size: int
    sha256: str
    detail: Literal["auto", "low", "high", "original"] = "auto"


class ApiMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[TextPart | ImagePart]
    ...
```

规则：

- 纯文本消息继续使用原始 `str`，逐字节保持当前兼容与 cache prefix 稳定性。
- 只要消息含图片，`content` 使用 part list；非空文字放在第一项，随后按 attachment
  顺序追加图片。
- 支持多图。
- 支持纯图片消息；此时没有空 `TextPart`。
- 文字和图片不能同时为空。
- system、assistant、tool 在第一阶段不得包含 `ImagePart`。
- LLM 层只携带 canonical base64；Data URL 只在 provider 发送前临时构造。

### Responses opaque provider state

Responses 在 `store=false` 或 ZDR 场景延续 reasoning 上下文时，需要请求
`include=["reasoning.encrypted_content"]` 并原位重放服务端返回的 reasoning Item。
Taifeng 用通用 envelope 保存这一不透明状态：

```python
class ProviderStateEnvelope(BaseModel):
    provider: str
    protocol: str
    item_type: str
    payload: dict[str, JsonValue]
```

OpenAI Responses 只允许持久化经过白名单投影的 `id`、`type`、`encrypted_content`、
`summary` 与必要 status 字段，不保存整个原始 response。该 envelope 附着在现有
`reasoning` conversation item 上并保持 provider 输出顺序。

provider 通过内部 `ResponseEvent(kind="provider_state")` 把它交给 TurnRunner；TurnRunner
必须在公开 `EventMsg` 分发前截获并持久化，禁止把 `encrypted_content` 放入 console、
JSONL telemetry、OTel 或业务 SSE。可见 reasoning summary 仍走现有
`reasoning_delta`/`reasoning` 文本路径。

## prompt 与历史转换

`history_to_api_messages()` 负责将 `user_message.attachments` 转为 `ImagePart`。转换前必须
重新执行完整 canonical 校验，确保从旧 JSONL、冷恢复或外部 store 读取的数据不能绕过
admission。

同一条 user message 是不可拆分的多模态原子：

```text
user_message(text, attachments)
        ↓
ApiMessage(role="user", content=[TextPart?, ImagePart...])
```

Responses opaque reasoning Item 不塞入 `ApiMessage.content`。`ApiRequest` 增加有序
`provider_state`，Responses adapter 在对应 assistant/function output 之前原位重建 Item；
Chat 与其他 provider 忽略非本协议 envelope，并且不得把它转成文本。

## OpenAI Chat 协议

`OpenAIChatClient` 调用 `{base_url}/chat/completions`。图片映射为：

```json
{
  "type": "image_url",
  "image_url": {
    "url": "data:image/png;base64,...",
    "detail": "auto"
  }
}
```

其他转换保持 Chat 语义：

- `ApiRequest.system_prompt` 继续生成前置 system messages。
- tool schema 为 `{"type":"function","function":{...}}`。
- assistant 工具调用使用 `tool_calls`，结果使用 role=`tool` 与 `tool_call_id`。
- Structured Outputs 使用 `response_format.json_schema`。
- `max_output_tokens` 翻译为当前模型支持的 Chat 字段；不在本变更统一迁移所有旧
  compatible 网关字段。
- OpenAI 专用 Chat 默认发送 `store=false`。
- SSE 继续归一化为现有 `ResponseEvent`。

`OpenAICompatClient` 不改成 `OpenAIChatClient` 的别名。它保留当前 payload 最小集合和
兼容容错，不强制第三方网关接受 `store` 或 OpenAI 新字段；内部可以复用无行为差异的
parser/helper。

## OpenAI Responses 协议

### 请求组装

`OpenAIResponsesClient` 调用 `{base_url}/responses`，固定：

```json
{
  "store": false,
  "stream": true,
  "include": ["reasoning.encrypted_content"]
}
```

它不发送 `previous_response_id` 或 Conversations API id。每次调用都根据 Taifeng logical
history 手工重放有序 Items，因此删除 provider 端状态、进程重启或从 JSONL 冷恢复不会改变
事实源。

图片映射为：

```json
{
  "type": "input_image",
  "image_url": "data:image/png;base64,...",
  "detail": "auto"
}
```

文本映射为 `input_text`。assistant 历史重建为 output message Item；工具调用与结果分别为：

```json
{"type":"function_call","call_id":"...","name":"...","arguments":"..."}
{"type":"function_call_output","call_id":"...","output":"..."}
```

tool schema 使用 Responses 扁平格式：

```json
{
  "type": "function",
  "name": "tool_name",
  "description": "...",
  "parameters": {},
  "strict": true
}
```

Structured Outputs 使用 `text.format`，不能沿用 Chat 的 `response_format`。system prompt
按稳定顺序转换为 input 前缀 message Items，避免合并字符串改变既有分段语义。

### reasoning 与工具循环

如果 response 同时返回 reasoning、assistant message 和 function call，持久化顺序保持
provider output 顺序。下一轮必须重放加密 reasoning Item、function call 与 matching
`function_call_output`。缺失 reasoning state、重复 `call_id`、孤立 output 或协议不匹配均在
网络前报确定性 history error；不能静默丢弃后继续调用。

压缩删除一轮历史时，reasoning、assistant、function calls 与 outputs 按配对边界原子删除。
保留尾部时必须完整保留该组。

### Responses SSE 归一化

| Responses SSE | Taifeng `ResponseEvent` |
|---|---|
| `response.created` / `response.in_progress` | 单次 `created` + `server_model` |
| `response.output_text.delta` | `text_delta` |
| `response.function_call_arguments.delta` | `tool_call_delta` |
| function call arguments/item done | 单次 `tool_call_done` |
| reasoning summary text delta | `reasoning_delta` |
| reasoning item with encrypted content | 内部 `provider_state` |
| `response.completed` | usage/cache + `completed` |
| `response.failed` / error event | 分类后的 `error` 并抛对应 `LLMError` |
| `response.incomplete` | 按 incomplete reason 分类，不伪装成功 |

同一 `call_id` 无论收到 arguments done 与 output item done 中的哪一种组合，都只能产生一次
`tool_call_done`。`completed.end_turn` 在存在 function call 时为 `False`。

## 图片 admission 与资源策略

```python
@dataclass(frozen=True)
class ImageInputPolicy:
    enabled: bool
    max_images: int
    max_item_bytes: int
    max_total_bytes: int
    allowed_media_types: frozenset[str]
```

该策略由业务按 EnginePool 注入。所有数值必须为正，`max_total_bytes` 不得小于
`max_item_bytes`；允许类型只能是 Taifeng 支持集合的子集。OpenAI 官方当前上限为单请求
512 MB total payload、最多 1,500 张图片，但这是 provider ceiling，不是 Taifeng 业务默认值。
有效限制取业务策略、`ContextBudget.max_request_bytes` 与 provider ceiling 的最小值。

admission 依次执行：

1. O(1) encoded-length gate，避免超大正文进入完整 base64 扫描。
2. canonical base64 校验。
3. decoded size 与累计 decoded total 校验。
4. SHA-256 校验。
5. MIME allowlist 与文件 signature 一致性校验。
6. 解析宽高；拒绝零尺寸、损坏头和超出安全整数范围的尺寸。
7. GIF 解析 image descriptor，只有一个 frame 才接受。
8. image count 校验。

格式检查使用有界纯 Python inspector，不引入同步文件 IO，不把正文写入临时文件。长时或大体积
解码受业务 byte policy 严格界定，并在进入 actor 的第一个 effect 前完成。

## token 与请求字节估算

### InputCostEstimator

context 层新增可注入的 `InputCostEstimator` 协议，输入 model、media type、宽高、detail，
输出保守 image token estimate。文本估算保持现有算法。

OpenAI estimator 按官方 patch/tile 规则实现已登记模型；GPT-5.6 的 `auto` 与 `original`
按官方当前 sizing 规则估算。未知模型/detail 组合使用业务配置的保守 ceiling，而不是返回 0。
估算值用于 soft/hard limit 与 compaction trigger；provider 返回的真实 usage 仍是计费和台账事实。

### wire bytes

`estimate_item_bytes()` 必须计入 canonical base64 长度和 JSON 结构开销。provider 在网络发送前
对最终序列化 JSON 做一次精确 UTF-8 bytes 检查，从而覆盖 Data URL 前缀、转义和约 4/3 base64
膨胀。超限时抛 `RequestTooLargeError`，不等待 provider 413。

## compaction、cache 与恢复

- 图片与所属 user text 是单条原子 item，不允许只摘要文字而仍发送图片，或保留文字却丢图。
- sliding/surgical/handoff 删除旧图片消息时，只改变 logical history；append-only JSONL 仍保留
  原始 canonical base64 和 lineage。
- preserve tail 命中的图片消息必须完整保留。
- Responses reasoning/function group 按配对边界原子保留或删除。
- 压缩仍必须返回 `cache_invalidated` 与 `anchor_preserved_until`。
- 图片消息进入稳定前缀后，其 canonical base64 与 part 顺序不得在重放时改变，否则视为
  cache break。
- 冷恢复从 JSONL 重新校验图片并产生与热路径相同的 `ApiMessage` 和 provider payload。
- provider state 不可被摘要成自然语言替代；需要删除时随所属采样轮一起删除。

## 错误语义

| 稳定 kind | 触发 | retryable |
|---|---|---|
| `unsupported_modality` | 业务未启用图片或所选 client 不支持图片 | false |
| `invalid_image` | base64、digest、signature、尺寸或 GIF frame 非法 | false |
| `image_count_exceeded` | 图片数量超过业务/provider 限制 | false |
| `attachment_too_large` | 单图或 decoded total 超限 | false |
| `request_too_large` | 最终 wire JSON 超限或 provider 413 | false |
| `unsupported_image_detail` | model/protocol 不支持请求 detail | false |
| `invalid_history` | Responses Item/reasoning/tool 配对不完整 | false |
| `transient_network` | timeout、transport、可重试 5xx/429 | true |

provider 400 中能稳定识别 modality/detail/context 的错误映射到对应 kind；无法稳定识别的保留
现有 `invalid_request`。任何 detail 不得自动从 `original` 降级到 `high/auto`。

## 可观测与敏感数据边界

现有 LLM attempt/turn 事件增加以下聚合字段：

- `input_modalities`
- `image_count`
- `image_total_decoded_bytes`
- `image_media_types`
- `image_details`
- `estimated_image_tokens`
- `openai_protocol=chat|responses`

所有 sink、异常和 request descriptor 默认禁止包含：

- attachment `content`
- Data URL
- 图片 bytes
- `encrypted_content`
- provider file id 或可下载 URL

strict audit 的完整 `conversation_item` 按既有 durable contract 保存 canonical attachment 正文；
telemetry 与业务事件仍只暴露聚合描述。若启用现有全文 request capture，图片正文也必须被专用
redaction 替换为包含 count/bytes/media/detail 的 descriptor，不能沿用普通 JSON 原样捕获。

## 测试设计

### schema 与 prompt

- 合法 PNG/JPEG/WebP/单帧 GIF。
- 非 canonical base64、size/digest 不匹配、MIME/signature 伪装、动画 GIF、空图片。
- 单项、总量、数量与最终 request bytes 边界。
- text-only content 保持原 `str`。
- text+single image、text+multiple images、image-only。
- text-first、attachment order 稳定。
- text/image 同时为空拒绝。
- 旧 JSONL 无 detail 按 `auto` 恢复。

### provider wire

- Chat `image_url` Data URL、tool schema、tool result、Structured Outputs 与 `store=false`。
- Responses `input_image`、扁平 function schema、function call/output、`text.format`、
  `include`、`store=false`，且不含 `previous_response_id`。
- 两协议分别覆盖 text/tool/image SSE 分片、usage、rate-limit、request-id、失败、incomplete、
  cancellation 与重复 done 去重。
- `OpenAICompatClient` 现有 payload 和 兼容端点测试保持通过。

### Sim conformance 与集成

Sim ledger 增加 provider-neutral part inspection，能断言图片数量、media/detail/digest 与顺序，
但不模拟视觉理解。集成测试覆盖：

```text
UserMessage
→ JSONL durable item
→ history_to_api_messages
→ Chat/Responses mock transport payload
→ ResponseEvent
→ assistant/function history
```

关闭并重新创建 Engine 后，从 JSONL 冷恢复并断言 payload 等价。另覆盖图片消息压缩、cache anchor、
Responses encrypted reasoning + tool call/output 配对、无正文 telemetry。

### 真实 GPT-5.6

仓库增加固定、可审核的小尺寸图片 fixture，图片内包含 prompt 未透露的唯一视觉验证码与明确
图形。真实验收不以 HTTP 200 为通过条件，必须验证模型提取的验证码/结构化字段。

定向场景：

- OpenAI Chat：单图识别、多图顺序、图片驱动 function call。
- OpenAI Responses：同样三项；另加 `store=false` encrypted reasoning 手工重放、工具结果续轮
  与跨进程冷恢复。
- 每个场景检查 `turn_completed`、server model、usage、tool events、protocol tag 与敏感数据
  redaction。

基础层红线执行顺序：

1. `PYTHONPATH=src uv run python examples/real_llm/selfcheck.py`
2. `PYTHONPATH=src uv run pytest tests/ -v`
3. GPT-5.6 + Responses 完整 `examples/real_llm/capability_matrix.py`
4. GPT-5.6 Chat/Responses 定向图片矩阵
5. 提交自动更新的 `docs/real-llm-ledger.{json,md}`

`docs/capability-matrix.md` 增加图片输入能力及 Chat/Responses 分列真实验证链接。台账记录
provider、protocol、model、commit、fixture digest、场景结果和失败原文的安全分类。

## 文档与兼容义务

实现前先新增 `docs/architecture/capabilities/llm-image-input.md` EARS 契约；实现完成后同步：

- `docs/architecture/llm-client.md`
- `docs/architecture/agent-loop.md`
- `docs/architecture/context-compression.md`
- `docs/architecture/conversation.md`
- `docs/architecture/overview.md`
- `docs/architecture/capabilities/README.md`
- `docs/capability-matrix.md`
- `docs/configurable-knobs.md`

公共导出新增 `ImageAttachmentV1`、`ImageInputPolicy`、`TextPart`、`ImagePart`、
`OpenAIChatClient` 与 `OpenAIResponsesClient`。`OpenAICompatClient` 导入路径、构造参数、
Chat SSE 行为与 DeepSeek 子类关系保持兼容。

## 完成判据

只有以下条件同时满足才能报告任务完成：

- provider-neutral 图片契约和两套 OpenAI wire adapter 已实现。
- 自动化测试全量通过。
- OpenAI Chat 真实图片定向验收通过。
- OpenAI Responses 真实图片、工具续轮与冷恢复验收通过。
- GPT-5.6 Responses 全能力矩阵通过。
- capability matrix、architecture 文档和 real LLM ledger 已同步并提交。

代码实现、自动化测试、真实 Chat 验收、真实 Responses 验收与台账状态在交付报告中分别列出，
不得互相替代。
