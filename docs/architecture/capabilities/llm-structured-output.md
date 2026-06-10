# llm-structured-output Specification

## Purpose
TBD - created by archiving change 2026-05-27-llm-structured-output. Update Purpose after archive.
## Requirements
### Requirement: ResponseFormatSpec 描述 LLM 强类型输出 schema

系统 SHALL 在 `taifeng.llm.types` 提供 `ResponseFormatSpec(BaseModel)`：

- `name: str` —— schema 名（必填，作为 LLM 看到的 tag）
- `json_schema: dict[str, Any]` —— JSON Schema dict（业务侧可用 `MyPydanticModel.model_json_schema()` 生成）
- `strict: bool = True` —— OpenAI strict mode（false 时允许字段缺失）

`ApiRequest` SHALL 新增字段 `response_format: ResponseFormatSpec | None = None`，默认 None 时行为完全不变（向后兼容）。

#### Scenario: ApiRequest 默认 response_format 为 None
- **WHEN** 业务构造 `ApiRequest(model="x", messages=[ApiMessage(role="user", content="hi")])`
- **THEN** SHALL 有 `req.response_format is None`

#### Scenario: ResponseFormatSpec 必填 name 与 json_schema
- **WHEN** 构造 `ResponseFormatSpec(name="UserProfile", json_schema={"type":"object","properties":{"id":{"type":"integer"}}})`
- **THEN** SHALL 通过，`strict` 默认 True

### Requirement: structured_output 事件标准化 LLM 强类型输出

系统 SHALL 在 `taifeng.llm.events` 提供：

- `EventKind` Literal 增加 `"structured_output"` 取值
- 工厂函数 `structured_output(*, parsed: dict | list, raw_text: str) -> ResponseEvent`

provider SHALL 在如下场景 emit `structured_output` 事件（且必须在 `completed` 之前）：

- 当且仅当 `request.response_format is not None`
- 且累积的文本能成功 `json.loads`
- emit 一次，data 形如 `{"parsed": <dict | list>, "raw_text": <full text>}`

解析失败 SHALL emit `error` 事件 `kind="parse_error", retryable=False`，并 **不** emit `structured_output`；后续仍 emit `completed`（让业务自决重试/回退）。

#### Scenario: 工厂返回正确事件结构
- **WHEN** `structured_output(parsed={"a": 1}, raw_text='{"a":1}')`
- **THEN** SHALL 返回 `ResponseEvent(kind="structured_output", data={"parsed": {"a": 1}, "raw_text": '{"a":1}'})`

### Requirement: openai_compat provider 适配 response_format

`OpenAICompatSession._build_payload` SHALL：

- 当 `req.response_format is None` → 不向 payload 加 `response_format` 字段（向后兼容）
- 当 `req.response_format is not None` → 加 `payload["response_format"] = {"type": "json_schema", "json_schema": {"name": <name>, "schema": <json_schema>, "strict": <strict>}}`

`OpenAICompatSession.stream` SHALL：

- 累积 text_delta 全文（method-local `full_text`）
- 流末若 `request.response_format is not None`：
  - try `json.loads(full_text)` 成功 → emit `structured_output(parsed=..., raw_text=full_text)`
  - 失败 → emit `error(kind="parse_error", retryable=False, message=f"structured_output_parse_failed: {exc}")`
- 两条路径都 SHALL 在最后 emit `completed`

#### Scenario: payload 含 response_format 字段
- **WHEN** ApiRequest 带 `response_format=ResponseFormatSpec(name="X", json_schema={"type":"object"})`
- **THEN** `_build_payload(req)["response_format"]` SHALL 等于 `{"type": "json_schema", "json_schema": {"name": "X", "schema": {"type":"object"}, "strict": True}}`

#### Scenario: 有效 JSON 响应 emit structured_output
- **WHEN** mock transport 返回 SSE 流的 text 内容是 `'{"id":1,"name":"alice"}'`
- **AND** request.response_format 非 None
- **THEN** 事件序列 SHALL 含 `structured_output` 且 `data["parsed"] == {"id":1,"name":"alice"}`
- **AND** 最后 SHALL emit `completed`

#### Scenario: 无效 JSON 响应 emit parse_error
- **WHEN** mock transport 返回非 JSON 文本（如 `"sorry, cannot comply"`）
- **AND** request.response_format 非 None
- **THEN** 事件序列 SHALL 含 `error(kind="parse_error", retryable=False)`
- **AND** SHALL **不**含 `structured_output`
- **AND** 最后 SHALL 仍 emit `completed`

### Requirement: litellm provider 适配 response_format

`LiteLLMSession.stream` SHALL：

- 当 `req.response_format is not None` → `kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": ..., "schema": ..., "strict": ...}}`
- 累积全文 + 流末 emit `structured_output` / `error(kind="parse_error")`（与 openai_compat 同语义）

LiteLLM 内部对 Anthropic / Gemini / OpenAI 自动桥接到各家 native 格式 —— Taifeng 仅传统一字段。

#### Scenario: LiteLLM kwargs 含 response_format
- **WHEN** ApiRequest 带 response_format，stream 调用 `litellm.acompletion(**kwargs)`
- **THEN** kwargs SHALL 含 `response_format = {"type": "json_schema", "json_schema": {...}}`

### Requirement: mock provider 支持 structured 字段

`SimTurn` SHALL 提供字段 `structured: dict | list | None = None`。

`MockSession.stream` 在 emit `completed` 之前 SHALL：

- 若 `request.response_format is not None and self._turn.structured is not None`：
  - emit `structured_output(parsed=self._turn.structured, raw_text=json.dumps(self._turn.structured, ensure_ascii=False))`
- 否则不 emit

#### Scenario: SimTurn.structured 配置时 emit
- **WHEN** `SimClient(turns=[SimTurn(text="", structured={"x": 1})])`
- **AND** ApiRequest 带 response_format
- **THEN** 事件流 SHALL 含 `structured_output(parsed={"x": 1})` 且在 `completed` 之前

#### Scenario: 未配置 structured 不 emit
- **WHEN** `SimTurn(text="hi")`（structured 默认 None）+ ApiRequest 带 response_format
- **THEN** 事件流 SHALL **不**含 `structured_output`

