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

注入入口固定在 Pool：

```python
await EnginePool.create(
    ...,
    image_input_policy=ImageInputPolicy(enabled=True, ...),
    input_cost_estimator=OpenAIImageCostEstimator(),
)
```

公共签名使用兼容默认值：

```python
image_input_policy: ImageInputPolicy | None = None
input_cost_estimator: InputCostEstimator | None = None
```

`image_input_policy=None` 解析为不可变 `DISABLED_IMAGE_POLICY`；文本路径不做图片检查或额外
解码。policy enabled 但 estimator 为 `None` 时使用
`ConservativeImageCostEstimator(policy.unknown_model_token_ceiling)`。因此现有 Pool、
custom client 与纯文本 `history_to_api_messages()` 调用不需要新增参数。

Pool 将两者逐层传给 `AgentEngine` / `TurnRunner`；`history_to_api_messages()` 的新增关键字参数
默认分别为 disabled policy、text-only capabilities 与 text-only estimator。运行时由 TurnRunner
显式传入解析值，不读取全局变量或环境变量。

`ModelClient` 增加只读能力描述：

```python
@dataclass(frozen=True)
class ModelCapabilities:
    input_modalities: frozenset[Literal["text", "image"]]
    provider: str
    protocol: str
    accepts_provider_state: bool = False
```

未实现该属性的旧 custom client 通过兼容 helper 解析为 text-only；现有 Anthropic、Gemini、
DeepSeek、LiteLLM 与 `OpenAICompatClient` 第一阶段均显式声明 text-only。`SimClient` 默认
text-only，测试可在构造时显式启用 image modality，以验证 provider-neutral conformance。
`OpenAIChatClient` 声明 text+image/chat，`OpenAIResponsesClient` 声明
text+image/responses 并接受 OpenAI Responses provider state。

TurnRunner 在转换前同时检查业务 policy 与 client capability。所有未改造 adapter 在序列化前再
执行 defense-in-depth part validation：遇到 `ImagePart` 必须抛 `unsupported_modality`；遇到
不属于当前 provider/protocol 的 `ApiProviderStateItem` 必须抛 `invalid_history`。不得把
Pydantic 对象或未知 dict 原样发送给 provider。

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

为了同时保持现有 Message provider 兼容与 Responses 精确 Item 顺序，`ApiRequest` 增加
provider-neutral 的有序历史视图：

```python
class ApiMessageItem(BaseModel):
    type: Literal["message"]
    role: Literal["system", "user", "assistant"]
    content: str | list[TextPart | ImagePart]
    sample_id: str | None = None
    output_index: int | None = None


class ApiFunctionCallItem(BaseModel):
    type: Literal["function_call"]
    call_id: str
    name: str
    arguments: str
    sample_id: str
    output_index: int


class ApiFunctionCallOutputItem(BaseModel):
    type: Literal["function_call_output"]
    call_id: str
    output: str
    origin_sample_id: str


class ApiProviderStateItem(BaseModel):
    type: Literal["provider_state"]
    sample_id: str
    output_index: int
    state: ProviderStateEnvelope


type ApiInputItem = (
    ApiMessageItem
    | ApiFunctionCallItem
    | ApiFunctionCallOutputItem
    | ApiProviderStateItem
)


class ApiRequest(BaseModel):
    messages: list[ApiMessage] = Field(default_factory=list)
    input_items: list[ApiInputItem] | None = None
    ...
```

validated `ApiRequest.input_items` 是规范源。构造规则为：

- 旧调用方只传 `messages`：validator 用单一 `messages_to_input_items()` 生成不含 provider
  state 的 ordered Items。
- 新调用方只传 `input_items`：validator 用单一 `input_items_to_messages()` 生成 coalesced
  Message compatibility view。
- 两者都传：validator 从 `input_items` 重新派生 messages 并做完整结构比较；不一致时以
  `invalid_request` 拒绝。
- 两者都为空表示合法空 history。
- validation 完成后 `input_items` 永不为 `None`；任何 provider 不得自己决定选择哪一份输入。

`input_items_to_messages()` 不把 provider state 渲染成文本；它只生成 semantic compatibility
messages，opaque state 仍仅存在于规范 `input_items`，由 client capability gate 决定是否接受。

`_convert_history()` 只生成 ordered `input_items` 与 source index，再调用同一个
`input_items_to_messages()` 生成 compatibility view，不能维护第二套 history 扫描逻辑。
第一阶段 Responses 只消费规范 `input_items`，Chat/现有 provider 只消费从它派生的
`messages`。测试必须断言文字、call id、tool output 与图片次序等价。

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

Responses adapter 的 terminal `NormalizedOutputItem` 是显式 discriminated union：

- `reasoning(output_index, visible_text, state?)`
- `message(output_index, text)`
- `function_call(output_index, call_id, name, arguments)`

三种类型均为 frozen Pydantic model，拒绝额外字段；列表严格按唯一、递增
`output_index` 排列。

OpenAI Responses 只允许持久化经过白名单投影的 `id`、`type`、`encrypted_content`、
`summary` 与必要 status 字段，不保存整个原始 response。该 envelope 附着在现有
`reasoning` conversation item 的 payload 上并保持 provider 输出顺序：

```python
reasoning.payload = {
    "text": str,
    "summary": str,
    "provider_state": ProviderStateEnvelope | None,
}
reasoning.metadata = {
    "llm_sample_id": str,
    "provider_output_index": int,
}
```

Responses 的每个 reasoning output Item 各自落一条 reasoning conversation item；即使其
visible summary 为空，只要有 encrypted state 也必须落盘。assistant/function call Items 同样
携带 `llm_sample_id` 与 `provider_output_index`。后续 function call output 携带
`origin_llm_sample_id`，该值从 matching function call 复制，不能由列表邻接猜测。

strict audit 的 `_ReasoningItemPayload` 增加可选强类型 `provider_state`，旧记录缺失时为
`None`。serializer 对 metadata 中三个保留键 `llm_sample_id`、`provider_output_index`、
`origin_llm_sample_id` 做显式类型校验，projector/deserializer 原样 round-trip；其他既有
canonical metadata 保持兼容。这样 legacy JSONL、strict audit Journal 和 hot history 使用同一
数据形状。

provider 在 `response.completed` 后通过一次内部
`ResponseEvent(kind="normalized_output", data={"items": [...]})` 把完整有序列表交给
TurnRunner。该事件必须位于公开 `completed` 前，且只允许出现一次。TurnRunner 截获它并按
`output_index` 创建带 sample metadata 的 conversation Items；它不再尝试从 text/tool delta
反推 Responses output boundaries。`normalized_output` 不进入公开 `EventMsg`、console、JSONL
telemetry、OTel 或业务 SSE。可见 reasoning summary 仍可实时走
`reasoning_delta`，但 durable reasoning 以 terminal normalized Item 为准。

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

Responses opaque reasoning Item 不塞入 `ApiMessage.content`。它按
`provider_output_index` 进入 `ApiRequest.input_items`，Responses adapter 因而能在对应
assistant/function call 之前原位重建；不存在与 messages 脱节的 flat state list。Chat 与其他
provider 在历史不含 provider state 时正常使用 compatibility messages；一旦存在 foreign
`ApiProviderStateItem`，必须在网络前以 `invalid_history` fail closed，既不忽略也不转成文本。
因此同一 thread 从 Responses 切换到 Chat/其他 provider 需要先通过显式 compaction/rewind
移除完整 Responses sample group，不能隐式丢状态切换。

### durable sample 分组

每次 logical LLM sample 使用稳定分组键：

```text
{thread_id}:{submission_id}:turn:{turn_index}:llm:{iteration}
```

只有成功 attempt 的 normalized output 才使用该 `llm_sample_id` 进入 conversation history。
同一 sample 的 reasoning、assistant message、全部并行 function calls，以及这些 calls 后续产生的
function outputs 组成一个配对图。新记录一律依靠显式 sample metadata 分组，不依靠物理邻接。

兼容旧 JSONL 时，缺少 sample metadata 的 history 使用确定性窗口算法：

1. reasoning 只进入 `pending_reasoning`，尚不单独开窗。
2. 紧随其后的第一条 assistant 消费全部 pending reasoning 并开启同一个 sample window；它不会
   关闭 reasoning。
3. 如果 function call 在 assistant 前出现，它消费 pending reasoning 并开启 tool-only window。
4. 当前 window 收集后续 function calls；outputs 不改变窗口，并按全局唯一 `call_id` 关联。
5. user/system/compacted 或下一条 reasoning 关闭当前 window；无 pending reasoning 的下一条
   assistant 关闭旧 window 并开启新 window。
6. suspension/记账 marker 跨过保窗，resume 后 output 仍按 call id 回到原 window。

存在重复 call id、孤立 output、连续 reasoning 无法确定 output order 或其他无法唯一归属时判
`invalid_history`，不猜测归属。

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

`ApiRequest.reasoning_effort` 增加 `"none"`。GPT-5.6 Chat 请求同时含 tools 时，只允许
`reasoning_effort is None` 或 `"none"`；显式 `minimal/low/medium/high` 在网络前以
`unsupported_combination` 拒绝，不静默改写。真实 Chat 图片驱动 function-call 场景显式使用
`reasoning_effort="none"`。Responses 不受该 Chat 限制，仍按模型能力验证 effort。

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

### attempt 缓冲与原子提交

每次网络 attempt 创建独立 `ResponsesAttemptAccumulator`。它按 output index 缓冲 typed output
Items、visible text、function arguments 与 encrypted reasoning state。流式 text/tool delta 可以按
现有 UI 语义发布，但只是 preview，不能直接写 conversation history、决定 final assistant text 或
触发 tool execution。

- 收到 `response.completed` 后才 finalize normalized Items。
- accumulator finalize 后先发唯一内部 `normalized_output`，再发公开 `completed`；TurnRunner
  必须收到 normalized output 才能把 Responses attempt 视为可提交，缺失/重复均为
  `invalid_response`。
- terminal normalized message Items 按 output index 拼接为本轮 final assistant text；terminal
  normalized function calls 是 tool dispatch 的唯一输入。`tool_call_done` 只作 UI preview。
- accumulator 对已观察到的 text/argument delta 分别聚合，并与 terminal normalized Item 做逐字节
  比较；不一致时 `invalid_response`，不得提交或执行工具。某 Item 完全没有 delta 时允许直接采用
  terminal 值。
- 成功 attempt 的 checkpoint 可以包含白名单化 provider state，随后
  `llm_response_committed + reasoning/assistant/function_call conversation_item*` 作为现有单一
  原子 batch 提交；只有该 batch 的 durable ack 才推进 hot history/projector。
- `response.failed`、`response.incomplete`、取消或 transport failure 必须销毁本 attempt
  accumulator；不得生成 conversation item，也不得把其中 encrypted state 带入下一次 retry。
- retry 创建全新的 accumulator。先前 attempt 即使发出过 UI delta，也不能成为 logical history。
- 成功 checkpoint 后、final conversation batch 前崩溃时，strict audit 仍只把 checkpoint 当作
  recovery evidence；未提交的 conversation item 不进入 LLM replay。恢复协调必须以 final
  `llm_response_committed` batch 为可见边界。

非 audit JSONL 路径同样只在完整 `response.completed` 后追加整组 Items；失败/incomplete 不追加
部分 reasoning 或 tool call。完整组还必须使用下述 commit frame 获得 crash-atomic visibility。

### 默认 JSONL batch visibility

新增可选 `AtomicBatchMessageStore` capability：

```python
async def append_atomic_batch(
    items: Sequence[ResponseItem], *, batch_id: str
) -> BatchAppendAck: ...
```

默认 JSONL store 实现该协议；Responses client 启用时，custom store 若不实现则在 Pool 构造期以
`unsupported_persistence_capability` 拒绝。现有 `append_batch(items)` 及非 LLM batch 完全不变。
只有 terminal normalized LLM response group 调用 atomic 方法，`batch_id=llm_sample_id`。
transport frame 不新增 conversation `ItemKind`：

```json
{"frame":"item_batch_begin","frame_id":"...","batch_id":"...","item_ids":["..."],"digest":"..."}
{"kind":"reasoning", "metadata":{"commit_batch_id":"..."}, "...":"..."}
{"kind":"assistant_message", "metadata":{"commit_batch_id":"..."}, "...":"..."}
{"frame":"item_batch_commit","frame_id":"...","batch_id":"...","item_ids":["..."],"digest":"..."}
```

- stable `batch_id` 提供业务幂等性；每次物理写 attempt 使用新的 transport `frame_id`。item ids、
  顺序与 canonical digest 在 begin/commit 中必须一致。
- writer 在同一个 append lock 内写 begin、全部 item lines、commit，完成 flush/fsync 后才返回 ack；
  hot history 只在 ack 后推进。
- reader 对旧版无 frame lines 保持立即可见。对带 `commit_batch_id` 的新 lines 先按 batch 缓冲，
  只有读到 frame id 匹配且 digest 正确的 commit 才一次性发布全部 Items。
- EOF、损坏行、缺项、digest 不符或只有 begin 没有 commit 的 batch 全部不可见；后续合法 batch
  仍可继续读取。orphan frame 不转换为 conversation item。
- committed batch index 以 `batch_id → digest/item_ids` 重建。重试已 committed 的相同 batch/digest
  返回 `already_committed=True` 的成功 ack，不再追加；相同 batch id 但内容不同抛
  `BatchConflictError`。orphan attempt 后重试可写新 frame id；reader 只发布第一个合法 committed
  frame，并按 stable batch id 去重。
- reasoning、assistant 和 function calls 属于 LLM response batch；后续 tool outputs 可使用现有
  tool outcome 原子边界单独提交，但必须带 `origin_llm_sample_id`。
- strict audit 继续使用 Journal 原子 batch，不重复增加 JSONL frame；projector 只有在 Journal ack
  后物化完整 batch。

这样进程在任意 line boundary 崩溃时都不会向 cold resume 暴露部分 encrypted reasoning 或孤立
function call，同时保持 append-only 与旧 transcript 可读。

### Responses SSE 归一化

| Responses SSE | Taifeng `ResponseEvent` |
|---|---|
| `response.created` / `response.in_progress` | 单次 `created` + `server_model` |
| `response.output_text.delta` | `text_delta` |
| `response.function_call_arguments.delta` | `tool_call_delta` |
| function call arguments/item done | 单次 `tool_call_done` |
| reasoning summary text delta | `reasoning_delta` |
| `response.completed` | 内部 `normalized_output`，随后 usage/cache + `completed` |
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
    unknown_model_token_ceiling: int = 32_768
```

该策略由业务按 EnginePool 注入。所有数值必须为正，`max_total_bytes` 不得小于
`max_item_bytes`；`unknown_model_token_ceiling` 必须为正；允许类型只能是 Taifeng 支持集合的
子集。32,768 是未知模型单图的默认保守估值，业务可按所选模型调高。OpenAI 官方当前上限为单请求
512 MB total payload、最多 1,500 张图片，但这是 provider ceiling，不是 Taifeng 业务默认值。
有效限制取业务策略、`ContextBudget.max_request_bytes` 与 provider ceiling 的最小值。

admission 依次执行：

1. O(1) image count gate，超量时不查看任何正文。
2. O(1) encoded-length gate，避免超大正文进入完整 base64 扫描。
3. canonical base64 校验。
4. decoded size 与累计 decoded total 校验。
5. SHA-256 校验。
6. MIME allowlist 与文件 signature 一致性校验。
7. 解析宽高；拒绝零尺寸、损坏头和超出安全整数范围的尺寸。
8. GIF 解析 image descriptor，只有一个 frame 才接受。

格式检查使用有界纯 Python inspector，不引入同步文件 IO，不把正文写入临时文件。长时或大体积
解码受业务 byte policy 严格界定，并在进入 actor 的第一个 effect 前完成。

### admission 时点

strict audit 与 legacy submit 共用 `prepare_user_message()` canonicalizer：

- disabled policy、client 不支持 image、非法 base64/digest/signature、数量/大小超限在 enqueue
  和任何 durable acceptance 之前拒绝。
- strict audit 写安全 `submission_rejected`，不写 `submission_accepted` 或 user conversation item。
- legacy 路径返回 rejected submission，不 append MessageStore、不进入 actor queue。
- 因此结构非法或业务禁用的图片不会污染 JSONL，也不会在每次 cold resume 重复失败。

model/detail 兼容可能依赖 provider 当前模型登记。canonical 图片可先 durable accepted；
provider preflight 若确认该组合不支持，则在网络前以 `unsupported_image_detail` 结束本 turn。
该合法 user item 保留在 history，业务切换支持该 detail 的 client/model 后可以重新执行。provider
只有在本地能力表无法确定时才可能返回等价 400，仍为 non-retryable。

## token 与请求字节估算

### InputCostEstimator

context 层新增可注入的 `InputCostEstimator` 协议，输入 model、media type、宽高、detail，
输出保守 image token estimate。文本估算保持现有算法。

OpenAI estimator 按官方 patch/tile 规则实现已登记模型；GPT-5.6 的 `auto` 与 `original`
按官方当前 sizing 规则估算。未知模型/detail 组合使用
`ImageInputPolicy.unknown_model_token_ceiling`（默认每图 32,768），而不是返回 0。
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
- Responses reasoning/function group 按 `llm_sample_id` 与 `origin_llm_sample_id` 配对图原子保留
  或删除。任何策略提出单一 candidate `replaced_range=[start,end)` 后，公共 boundary resolver
  计算所有相交 sample groups 的传递闭包，闭包仍表示一个连续 range；中间 suspension/记账项随该
  连续 closure 一起处理，不产生多段替换。
- preserve tail、当前 live suspension 与 pinned state 的 protected range 优先级最高。如果 closure
  与 protected range 相交，resolver 不向 protected range 扩展，而是把 candidate `end` 收缩到最早
  相交 sample group 的起点，完整保留该 group 及其间所有 Items；随后重新计算闭包。若收缩后无
  可删除内容，本次压缩 no-op，并由现有 resource/overflow 语义决定是否失败。
- 不与 protected range 相交时，resolver 才向两侧扩展到完整 closure。多个并行 calls、交错
  outputs、suspension marker 与 resume 后补写 output 均按显式 metadata/call id 纳入同组。无法
  唯一成组时压缩 fail closed，不修改 history。
- 旧 history 使用“durable sample 分组”一节的兼容算法计算临时 group；所有可能 cut position 都有
  pair-safety 参数化测试。
- handoff/summary model 只能接收专用 `CompactionView`：reasoning 仅投影视觉可见的 `text` 与
  `summary`，显式删除 `provider_state`、`encrypted_content` 和保留 metadata。禁止通过
  `str(payload)` 或通用 `model_dump()` 构造 compaction prompt。被移除 group 的 opaque state 直接
  消失，不得编码进自然语言 summary；被保留 group 的 state 原样留在 history。
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
| `unsupported_combination` | Chat tools 与不支持的 reasoning effort 组合 | false |
| `unsupported_persistence_capability` | Responses 使用的 custom store 缺少 atomic batch | false |
| `invalid_history` | Responses Item/reasoning/tool 配对不完整 | false |
| `invalid_response` | Responses 缺失/重复 terminal normalized output 或 Item 边界非法 | false |
| `batch_conflict` | 相同 stable batch id 对应不同 digest/items | false |
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
- `ApiRequest` messages-only、items-only、两者等价与两者冲突四种构造路径。
- Pool/history helper 省略新参数时，现有 text-only 行为逐字节不变。

### provider wire

- Chat `image_url` Data URL、tool schema、tool result、Structured Outputs 与 `store=false`。
- Responses `input_image`、扁平 function schema、function call/output、`text.format`、
  `include`、`store=false`，且不含 `previous_response_id`。
- 两协议分别覆盖 text/tool/image SSE 分片、usage、rate-limit、request-id、失败、incomplete、
  cancellation 与重复 done 去重。
- Responses SSE 覆盖多个 output Items、reasoning/message/并行 function calls 交错，以及唯一
  terminal `normalized_output` 的 index/order。
- Responses history 切换到 Chat/其他 provider 时稳定 `invalid_history`；移除完整 Responses
  sample group 后可切换。
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

默认 JSONL 对 batch begin、每条 item、commit 与 fsync 前后逐边界注入 crash；恢复时只有完整且
digest 匹配的 committed batch 可见，并覆盖 ack 丢失后的同 digest no-op 与异 digest conflict。
压缩对每个 candidate cut position 参数化验证 sample closure 与 preserve-tail 优先级；用 sentinel
encrypted state 断言 compaction prompt、summary、日志和 capture 均不含该值。

### 真实 GPT-5.6

仓库增加固定、可审核的小尺寸图片 fixture，图片内包含 prompt 未透露的良性库存序列号与明确
几何图形；它不是 CAPTCHA，也不使用验证/绕过措辞。真实验收不以 HTTP 200 为通过条件，必须
验证模型提取的序列号/结构化字段。

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
