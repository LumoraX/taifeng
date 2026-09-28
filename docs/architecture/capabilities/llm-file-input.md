# Capability: llm-file-input（预留协议）

## Purpose

用户消息附带文件（PDF 等文档）直接交给模型阅读。主流 provider 均已支持：OpenAI Chat `{"type": "file"}` / Responses `input_file`、
Anthropic `document` 块、Gemini `inline_data`。内核目前只支持图片输入（[llm-image-input](llm-image-input.md)）。

状态：**预留**（Reserved）——本契约先固定数据形状与门控规则，**尚无实现**。不在代码里先放一个无人产出、各 provider 全拒绝的类型：
那是死代码，且把新 part 加进 content union 需要同步改所有 provider 的映射分支，应与真实输入路径一起落地。关联：ADR 0067。

## 数据契约（实现时遵循）

- `FileAttachmentV1`（canonical，落 JSONL）：`media_type`（白名单，首批 `application/pdf`）、`data`（canonical base64）、
  `filename`（可选，展示用）、`sha256`、`size_bytes`。与 `ImageAttachmentV1` 同构：admission 在 durable append 之前完成。
- `FilePart`（`PartContent` 成员）：由 prompt 重建从 `FileAttachmentV1` 投影；**不存在业务侧直接传 provider 原生形状的透传路径**。
- `ModelCapabilities.input_modalities` 增加 `"file"`；默认不含——与图片同规矩，能力一律显式打开。
- `FileInputPolicy`（与 `ImageInputPolicy` 对齐）：总闸默认关闭；`max_files`、`max_item_bytes`、`max_total_bytes`、
  `allowed_media_types`。

## 行为契约（实现时遵循）

- 未声明 `"file"` 能力的 client 收到 `FilePart` SHALL 在序列化前抛 `UnsupportedModalityError`（与 `assert_text_only_request` 同一道门），
  不得静默丢弃或降级为文件名文本。
- 策略未开启时 `UserMessage` 带文件 SHALL 在 durable append 前拒绝。
- 审计 / request capture 中文件字节 SHALL 按图片同规则脱敏为 `media_type` / `size` / `sha256` 描述。
- token 估算须有非零上界（provider 按页计费，未知模型取保守上界），最终 wire 字节仍受 `ContextBudget.max_request_bytes` 门禁。
- 压缩：被压缩区间里的文件只保留描述占位（文件名、类型、大小），不进入摘要输入正文。

## Provider 映射（实现时遵循）

| Provider | wire 形状 |
| --- | --- |
| OpenAI Chat | `{"type": "file", "file": {"file_data": "data:<media_type>;base64,…", "filename": …}}` |
| OpenAI Responses / codex | `{"type": "input_file", "file_data": "data:…", "filename": …}` |
| Anthropic | `{"type": "document", "source": {"type": "base64", "media_type": …, "data": …}}` |
| Gemini | `{"inline_data": {"mime_type": …, "data": …}}` |
