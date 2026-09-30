# Capability: llm-file-input（用户消息文件输入）🧪

## Purpose

用户消息附带文件（首批只有 PDF）直接交给模型阅读。与 [llm-image-input](llm-image-input.md) 同构：
canonical 内联正文、业务显式开闸、client 显式声明能力、durable append 前 admission、prompt 重建时复核、
审计脱敏、保守成本估算、压缩只留描述占位。

状态：🧪 实验（入口在 `taifeng.experimental`，ADR 0066 分层）。决策记录：[ADR 0068](../../decisions/0068-user-file-input.md)
（落地 [ADR 0067](../../decisions/0067-reserved-protocols-fitness-file-input-routing.md) 的预留契约）。

**入口限于 user 消息。** 工具经 `function_call_output` 回传文件不在本契约内（工具附件契约
[tool-image-attachment](tool-image-attachment.md) 只有图片）；wire 层遇到非 user 消息里的 `FilePart` 一律
`InvalidHistoryError`。

## 数据契约

| 符号 | 位置 | 约束 |
| --- | --- | --- |
| `FileAttachmentV1` | `llm/file_input.py` | conversation 持久化形态（落 JSONL）：`kind="file"`、`media_type`（白名单，首批 `application/pdf`）、`size`（decoded 字节，>0）、`sha256`（小写 hex）、`encoding="base64"`、`content`（canonical base64，**不得**是 Data URL）、`filename`（可选展示名，1–255 字符，不含路径分隔符与控制字符）；`extra="forbid"`；`from_bytes()` 一次算对 base64 / size / sha256 |
| `FilePart` | `llm/types.py` | request 内的 provider-neutral 形态，`PartContent` 成员：`type="file"`、`media_type`、`base64_data`、`size`、`sha256`、`filename`；由 prompt 重建从 `FileAttachmentV1` 投影（`to_part()`）。**不存在业务侧直接传 provider 原生形状的透传路径** |
| `FileInputPolicy` | `llm/file_input.py` | 业务注入的总闸（默认关闭）：`enabled`、`max_files`（默认 1）、`max_item_bytes` / `max_total_bytes`（默认 10 MiB）、`allowed_media_types`（须是 `SUPPORTED_FILE_MEDIA_TYPES` 子集）、`page_token_ceiling`（默认 5000）、`unknown_file_token_ceiling`（默认 32768）；构造期拒绝不可执行配置 |
| `ModelCapabilities.input_modalities` | `llm/client.py` | 增加 `"file"`；默认不含——与图片同规矩，能力只由专用协议客户端显式声明，不得据模型名 / 域名推断。声明 `"file"` 的 client 派生 skill 模态标签 `input_file`（`requires.modalities` 可据此做 G4a 路由门控） |

字段名与 `ImageAttachmentV1` / 严格 Journal `AttachmentV1` 同构（`content` / `size` / `encoding`），
而不是预留稿里的 `data` / `size_bytes`：两类附件共用 canonical base64 解码（`llm/attachment_codec.py`）、
通用字节估算与审计 descriptor 遍历，字段同名才无需逐类特判。

`UserMessage.attachments` 是图片与文件共用的同一入口：元素为 `ImageAttachmentV1` / `FileAttachmentV1`
的 `model_dump()`，按 `kind` 区分。有附件时 user 消息内容为有序 parts：文字（非空时）在首项，
图片与文件**按提交顺序交错**排在其后。

## 行为契约

- 策略未开启时，带文件的 `UserMessage` SHALL 在 enqueue 与 durable append 前以 `unsupported_modality` 拒绝，
  不留下每次恢复都会报错的脏历史。
- 未声明 `"file"` 能力的 client 收到文件 SHALL 在序列化前抛 `UnsupportedModalityError`：prompt 层先拒
  （入队准入与每轮重建共用 `loop/attachment_parts.user_attachment_parts`），native adapter 再经
  `providers/_modality_gate.assert_request_modalities` 兜底（与图片同一道门）；不得静默丢弃或降级为文件名文本。
- admission（`admit_file_attachments`）依次校验：策略总闸 → 数量 → MIME 白名单 → canonical base64（编码长度
  O(1) 闸门先于解码）→ decoded size / SHA-256 与声明一致 → 累计字节 → PDF 结构（`%PDF-x.y` 头、末 1024 字节内
  `%%EOF`）。错误分类：`unsupported_modality` / `file_count_exceeded` / `attachment_too_large` / `invalid_file`，均不可重试。
- 冷恢复：JSONL 里的 canonical attachment 原样读回，prompt 重建按**当前**策略与 client 能力重新 admission；
  策略收紧、改为关闭或换到不支持文件的 client 时 fail closed（turn 以 `unsupported_modality` 失败），不静默丢弃历史里的文件。
- strict audit（SessionJournal）模式下，文件附件以 `FileAttachmentRecordV1` 的形状进 `submission_accepted`（ADR 0095）：准入口径与非审计路径相同（模型支持、策略启用、大小与结构合法），另受 `AuditConfig` 的附件字节上限约束；不合格的带文件 `UserMessage` 在 acceptance 前 durable 拒绝（`submission_rejected`，不保存正文）。

## 成本与预算

- token 估算有非零上界（`context/budget.py`）：策略启用时复用 admission 数出 PDF 页对象数，估 `页数 × page_token_ceiling`；
  页对象藏在压缩 object stream 里数不到时取 `unknown_file_token_ceiling`；策略未启用（如冷恢复读到历史文件）时不解码正文，
  每个文件直接取 `unknown_file_token_ceiling`。首轮采样后由实测 usage 校准。
- `AgentEngine.estimate_tokens()` 与 turn 内预算判定共用同一 `FileInputPolicy`。
- 最终 wire 字节仍受 `ContextBudget.max_request_bytes` 精确门禁（OpenAI / Codex）；`estimate_item_bytes` 把附件正文计入。

## 脱敏

- 普通 request capture（`redact_sensitive_request_data`）：`FilePart` 替换为
  `{type, media_type, size, sha256, filename, content_redacted: true}`，base64 与 Data URL 绝不出现。
- strict attempt 投影（`project_attempt_request`）：删除 `base64_data`，写 `content_redacted` marker，redaction
  manifest 记一条 `kind="file_base64"`（`RedactionEntryV1` 接受该值）；canonical attempt digest 仍绑定脱敏前正文。

## 压缩

被压缩区间里的 user 消息，文件只以描述占位进入摘要输入：`[附件文件 <filename|（未命名）>（<media_type>，<size> 字节）]`；
正文不进入 compaction prompt。未被压缩的尾部消息原样保留附件。

## Provider 映射

Data URL 只在网络边界临时构造；`filename` 缺省时 OpenAI 系用确定性默认名 `attachment-<sha256 前 12 位>.pdf`
（同一文件每轮 wire 逐位一致，前缀缓存稳定）。

| Provider | 能力声明 | wire 形状 |
| --- | --- | --- |
| OpenAI Chat（`OpenAIChatClient`） | `text / image / file` | `{"type": "file", "file": {"file_data": "data:<mime>;base64,…", "filename": …}}` |
| OpenAI Responses（`OpenAIResponsesClient`） | `text / image / file` | `{"type": "input_file", "file_data": "data:…", "filename": …}` |
| Codex（`CodexResponsesClient`） | `text / image / file` | 同 Responses 的 `input_file` |
| Anthropic（`AnthropicClient`） | `text / file` | `{"type": "document", "source": {"type": "base64", "media_type": …, "data": …}, "title"?: filename}` |
| Gemini（`GeminiClient`） | `text / file` | `{"inlineData": {"mimeType": …, "data": …}}`（与该 provider 其余字段同用 camelCase；REST 同时接受 `inline_data`） |
| `OpenAICompatClient` / `DeepSeekClient` | text-only | 序列化前 `UnsupportedModalityError` |
| `LiteLLMClient` | text-only | 序列化前 `UnsupportedModalityError`：LiteLLM 虽能把 OpenAI `file` part 翻译给部分后端，但能否接受取决于 model 前缀路由到的具体后端，内核不据模型名猜能力 |
| `SimClient` | 构造参数显式给定 | 渲染为 `<file media_type=… filename=… size=… sha256=…>` 结构摘要，`RecordedRequest.file_inputs()` 暴露脱敏描述 |

## 业务接入

```python
from taifeng.experimental import FileAttachmentV1, FileInputPolicy

pool = await EnginePool.create(
    model_client=CodexResponsesClient(api_key=key, base_url=root),
    file_input_policy=FileInputPolicy(enabled=True, max_files=2, max_item_bytes=8 * 1024 * 1024,
                                      max_total_bytes=16 * 1024 * 1024),
    ...,
)
await engine.submit(UserMessage(
    text="总结附件",
    attachments=[FileAttachmentV1.from_bytes(pdf_bytes, filename="report.pdf").model_dump()],
))
```

## 验证边界

- CI（Sim / mock transport）：canonical 形态与 admission 边界、各 provider wire 形状与能力门控、脱敏（capture + strict
  manifest）、token 估算、压缩占位、JSONL 往返与冷恢复、真实 `EnginePool` + `SimClient` 端到端（`tests/llm/test_file_input*.py`、
  `tests/llm/test_modality_gate.py`、`tests/loop/test_file_input_wiring.py`、`tests/loop/test_attachment_parts.py`、
  `tests/context/test_file_input_accounting.py`）。不声称文档理解。
- `examples/real_llm/selfcheck.py` 含零消耗的 PDF 结构 / codex `input_file` wire / 脱敏预检。
- 真实 LLM：`examples/real_llm/test_codex_file_input.py`（`capability_matrix.py --provider codex` 的 provider 专属场景
  `codex_file_input`）——纯标准库生成只含唯一随机核对码的 PDF，断言模型经工具参数与最终回复逐字读出该码，且 capture /
  事件日志不含正文。OpenAI Chat / Responses、Anthropic、Gemini 的文件 wire 目前只有单测覆盖，未做真实验证。
