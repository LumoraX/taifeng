# ADR 0115：Gemini 的工具 schema 投影与 Google 错误体分类

- 状态：Accepted
- 日期：2026-09-30
- 关联：[llm-provider-native](../architecture/capabilities/llm-provider-native.md)；ADR 0074

## 背景

拿真实 Gemini 端点验证用户文件输入时，请求在发出的那一刻就被拒了，而且与文件无关：

1. **任何带默认工具的会话在 Gemini 上都发不出去。** `GeminiClient` 把工具的 `input_schema` 原样放进
   `functionDeclarations[].parameters`。Gemini 这个字段只认 OpenAPI 3.0 Schema 的子集，遇到不认识的
   关键字整次请求 400——而内核自己的 `read_skill`、`call_skill` 等内置工具的 schema 都带
   `additionalProperties`。此前的真实验证（ADR 0074）用的是一个手写的、恰好不带这个关键字的工具，
   没有撞上。
2. **这个 400 被归成了「内容被安全策略拦截」。** Google 的错误体把结构化细节放在 `error.details`，
   参数错误的标准字段叫 `fieldViolations`；通用分类对整个 body 做关键字匹配，`violat` 命中，于是一次
   参数错误被报成安全拦截，给出的处置建议（调整输入后重试）与真实原因毫不相干。

命中 ADR 0017 规则①。

## 决策

1. **工具 schema 在送出前投影成 Gemini 接受的形状**（`llm/providers/gemini_schema.py`）：按白名单保留
   Gemini `Schema` 对象的字段，其余去掉；`properties` 的键是属性名，不当关键字过滤；
   `type: ["string", "null"]` 改写为 `type: string` + `nullable: true`。输入不被改动。
2. **投影只影响「告诉模型参数长什么样」**。参数是否合规仍由内核按原始 schema 在派发前校验
   （tool-argument-validation）——去掉 Gemini 不认的约束不会放进不合规的调用，模型照不全的约束写出
   的参数会被内核拒回并让它重试。
3. **Google 错误体按 `error.status` + `error.message` 分类**，不看 `details`；错误文本仍是完整 body
   （排查要看 `fieldViolations`）。body 不是这个形状时退回通用规则。

## 不做

- **用 `parametersJsonSchema` 送完整 JSON Schema**：那是较新的字段，各模型与各 API 版本的支持不一；
  白名单投影对所有版本都成立。
- **在通用分类里去掉 `violat` 关键字**：它对其他 provider 的安全拦截文本仍然有用；问题出在对 Google
  的结构化 body 做全文匹配。
- **把 schema 投影做成所有 provider 共用的一层**：各家接受的子集不同（OpenAI strict 模式反而要求
  `additionalProperties: false`），各 provider 自己负责。

## 影响

- R1–R5：无影响。
- Gemini 上的参数错误从 `content_filter`（终态、建议调整输入）变为 `invalid_request`。

## 验证

- `tests/llm/test_gemini_provider.py` 新增 3 项：投影（不认的关键字、同名属性、可空类型、嵌套、输入不变）、
  内置工具的 schema 投影后不含 `additionalProperties`、`fieldViolations` 不再被当成安全拦截而真的安全
  拦截与上下文超长照旧归类。
- 真实端点（`gemini-3.1-pro-preview`，2026-09-30）：`examples/real_llm/e2e.py` 带默认工具的会话跑通
  （`read_skill` 被调用并完成两轮）；`tool_args_replay_verify.py` 两例仍通过；`file_input_verify.py`
  5 项通过。修复前第一条请求即 400。
