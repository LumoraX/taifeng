# LLM 图片输入能力契约

## 范围

本能力仅接受完整内联 canonical base64 的 PNG、JPEG、WebP 与非动画 GIF 图片输入。它不支持 URL、临时路径、file id、音频、视频、PDF 或图片生成。

## 数据契约

- 当用户消息不含图片时，系统必须保持 ApiMessage.content 为原始 str。
- 当用户消息含图片时，系统必须以有序 TextPart 和 ImagePart 列表表示内容；存在文字时文字必须在第一项，图片必须保持 attachment 顺序。
- 当消息只有图片时，系统不得生成空 TextPart。
- ImagePart 必须保存 MIME、canonical base64、decoded size、SHA-256 与 detail；provider Data URL 不得持久化或进入 conversation/context。
- ApiRequest.input_items 必须是请求历史的规范源；messages 只能由 items 确定性派生。两者同时传入且不一致时，系统必须拒绝请求。
- Provider state 必须保持为带 provider/protocol/item_type 的不透明 envelope，且不得渲染成自然语言消息。
- `ImageAttachmentV1` 是 conversation 的持久化形态；`ImagePart` 是 request 内的 provider-neutral 形态。两者都不接受 Data URL、URL、路径或 file id。
- `detail` 缺省为 `auto`，旧 JSONL attachment 缺该字段时必须按 `auto` 恢复。

## 启用与拒绝

- 当业务未注入 enabled ImageInputPolicy 时，系统必须在任何 durable acceptance 和网络请求前拒绝图片，错误 kind 为 unsupported_modality。
- 当客户端不声明 image capability 时，系统必须在网络请求前拒绝图片，且不得将 ImagePart 静默降级为文本。
- 当图片的 base64、size、digest、signature、尺寸、frame count 或资源上限不合法时，系统必须在入队前拒绝，且不得写入 user conversation item。
- 当客户端是旧 custom client 或 OpenAICompatClient 时，系统必须默认 text-only。

## OpenAI 协议

- OpenAIChatClient 必须调用 /chat/completions，使用 image_url 的临时 Data URL，并默认 store=false。
- OpenAIResponsesClient 必须调用 /responses，使用 input_image，默认 store=false，包含 reasoning.encrypted_content，且不得使用 previous_response_id 或 Conversations API。
- Responses 只可在 response.completed 后以唯一 normalized_output 形成 durable assistant text、reasoning、function call 和工具调度；流式 delta 仅是预览。
- Responses 的 `normalized_output` 必须恰好一次且先于唯一 `completed`；缺失、乱序或重复终态均不得提交。
- Responses reasoning 的 `encrypted_content` 随 sample 原子持久化并手工重放；Taifeng JSONL 是恢复事实源，不使用 `previous_response_id`。
- `OpenAICompatClient` 保持 text-only。图片能力只存在于 `OpenAIChatClient` 和 `OpenAIResponsesClient`，不能根据模型名自动打开。

## 持久化、压缩与可观测性

- Responses 成功输出组必须以 llm_sample_id 作为稳定 batch id 原子提交；冷恢复不得看见未 commit 的部分输出。
- 压缩必须按完整 sample group 删除或保留 reasoning、assistant、function call 与 tool output；compaction prompt 不得包含 provider_state、encrypted_content 或图片正文。
- telemetry、日志与 request capture 必须只暴露图片数、decoded bytes、MIME、detail 与估算 token，绝不得包含 base64、Data URL、图片 bytes 或 encrypted_content。
- `enable_request_capture=True` 仍保留文字 prompt，但每个图片 part 必须替换为 `content_redacted` 结构描述；OTel 继续整条跳过 request capture。

## 业务接入

图片默认禁用。业务必须同时选择专用 OpenAI 协议客户端并显式注入策略：

```python
pool = await EnginePool.create(
    model_client=OpenAIResponsesClient(api_key=key, model="gpt-5.6"),
    image_input_policy=ImageInputPolicy(
        enabled=True,
        max_images=4,
        max_item_bytes=10 * 1024 * 1024,
        max_total_bytes=20 * 1024 * 1024,
    ),
    input_cost_estimator=OpenAIImageCostEstimator(),
    ...,
)

await engine.submit(UserMessage(
    text="检查图片",
    attachments=[ImageAttachmentV1(...).model_dump()],
))
```

跨进程恢复必须把已知 `thread_id` 显式传给 `resume_thread_id`；`session_id` 只是进程内 engine cache key，不承担持久化映射。

## 验证边界

- CI 用 Sim 与 mock transport 验证 admission、wire、原子提交、strict audit 和 cold resume，不声称视觉理解。
- `examples/real_llm/selfcheck.py` 只做零消耗 fixture/wire/脱敏预检。
- `examples/real_llm/capability_matrix.py --provider openai --model gpt-5.6` 才运行 Chat/Responses 单图、多图顺序、图片工具、encrypted-state replay 与冷恢复语义断言，并更新真实台账。
- 没有真实凭据或 endpoint 不可用时必须登记为“未执行”，不得用单测替代真实验收。
