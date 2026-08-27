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

## 启用与拒绝

- 当业务未注入 enabled ImageInputPolicy 时，系统必须在任何 durable acceptance 和网络请求前拒绝图片，错误 kind 为 unsupported_modality。
- 当客户端不声明 image capability 时，系统必须在网络请求前拒绝图片，且不得将 ImagePart 静默降级为文本。
- 当图片的 base64、size、digest、signature、尺寸、frame count 或资源上限不合法时，系统必须在入队前拒绝，且不得写入 user conversation item。
- 当客户端是旧 custom client 或 OpenAICompatClient 时，系统必须默认 text-only。

## OpenAI 协议

- OpenAIChatClient 必须调用 /chat/completions，使用 image_url 的临时 Data URL，并默认 store=false。
- OpenAIResponsesClient 必须调用 /responses，使用 input_image，默认 store=false，包含 reasoning.encrypted_content，且不得使用 previous_response_id 或 Conversations API。
- Responses 只可在 response.completed 后以唯一 normalized_output 形成 durable assistant text、reasoning、function call 和工具调度；流式 delta 仅是预览。

## 持久化、压缩与可观测性

- Responses 成功输出组必须以 llm_sample_id 作为稳定 batch id 原子提交；冷恢复不得看见未 commit 的部分输出。
- 压缩必须按完整 sample group 删除或保留 reasoning、assistant、function call 与 tool output；compaction prompt 不得包含 provider_state、encrypted_content 或图片正文。
- telemetry、日志与 request capture 必须只暴露图片数、decoded bytes、MIME、detail 与估算 token，绝不得包含 base64、Data URL、图片 bytes 或 encrypted_content。
