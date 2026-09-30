# ADR 0074：Anthropic / Gemini 回放写坏的 tool call 参数时用显式标记，不静默改成 `{}`

- 状态：Accepted（Amends #0036、#0037）
- 日期：2026-09-30
- 关联：[llm-provider-native 契约 § tool-args-replay](../architecture/capabilities/llm-provider-native.md)；
  [llm-client 活文档](../architecture/llm-client.md)；[tool-whitelist 契约](../architecture/capabilities/tool-whitelist.md)；
  ADR 0036 / 0037 / 0047

## 背景

ADR 0036 把「坏参数静默变 `{}`」在派发层的四处解析点全部收口到 `parse_tool_arguments`，坏参数不再执行 handler。
同一 ADR 与 ADR 0037 都把 provider 侧的同类问题记为欠账：`anthropic_provider._to_anthropic_messages` 与
`gemini_provider._to_gemini_contents` 回放历史 tool call 时仍是 `except JSONDecodeError: args = {}`。

在 main（c3aa46b）核对，存在两个缺陷：

1. **非法 JSON 静默改成 `{}`**：模型在历史里看到「自己发过一次无参调用」，紧跟一条 `invalid_arguments` 错误结果。
   两者对不上，模型无从知道当初写错了什么，违反「禁止静默回退」。
2. **非对象 JSON 原样穿透**：`json.loads("[1, 2]")` 成功返回 list，被直接填进 `tool_use.input` / `functionCall.args`，
   provider 以 400 拒绝。历史不可改写，该会话此后每次请求都失败。

OpenAI 系协议把参数当字符串回放，没有这个问题。

## 决策

1. **显式标记对象**：解析失败时回放
   `{"__invalid_arguments__": <错误分类>, "__raw_arguments__": <原始文本>}`。错误分类沿用派发层文案
   （`invalid_json: ...` / `not_an_object: got <类型>`），模型看到的「发出的参数」与「收到的错误」相互对应。
2. **不抛异常**：历史是只追加的事实，回放阶段抛错等于让一次模型笔误永久毁掉整个会话。
3. **单一入口**：`llm/providers/_tool_args.replay_tool_arguments`，两家 provider 共用。不复用
   `loop/tool_batch.parse_tool_arguments`——llm 层不依赖 loop 层；两者职责也不同（派发前裁决 vs 回放时表达）。
4. **原始文本有上限**：超过 4000 字符截前缀并注明原长度。写坏的参数常是被截断的超长输出，不能无上限回灌上下文。
5. **可观测**：每次按标记回放记一条 warning 日志（工具名、错误分类、原始长度；不含原始文本，避免日志泄露参数内容）。
   该调用在派发时已产生 `invalid_arguments` 结果与对应事件，回放层不重复发 EventMsg。
6. **确定性**：输出只由输入决定，同一段历史多次回放逐字节一致，不破坏 provider 侧缓存前缀（R2）。

## 替代方案

- **抛分类异常（`InvalidRequestError`）**：最「不静默」，但会话从此不可用；否决。
- **从历史里剔除这对 tool call / 结果**：改写了模型的执行事实，模型也失去从错误中修正的依据；否决。
- **把原始文本塞进单键 `{"raw": "..."}`**：键名可能与真实工具的参数重名，模型与审计都分不清这是标记还是真参数；
  用双下划线包裹的专用键；否决。
- **在落史时就修正参数**：历史应如实记录模型产出；修正属于表达层，只在需要对象形态的 provider 上发生；否决。

## 后果

- 此前含写坏参数的会话在 Anthropic / Gemini 上的请求内容会变化（`{}` → 标记对象），对应位置之后的 provider 缓存
  失效一次；此后稳定。
- 含非对象参数的历史不再触发 provider 400。
- R1–R5：R1 无业务概念；R2 见决策 6；R3 见决策 5；R4 纯计算无长时操作；R5 不改持久化形状。
- 真实端点验证：Anthropic / Gemini 真实回归需要对应 key，未随本次变更执行，见台账。
