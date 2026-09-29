# ADR 0068：用户消息文件（PDF）输入——与图片输入同构落地

- 状态：Accepted
- 日期：2026-09-29
- 关联：[llm-file-input](../architecture/capabilities/llm-file-input.md)；[llm-image-input](../architecture/capabilities/llm-image-input.md)；ADR 0066 / 0067
- Supersedes：ADR 0067 决策 2「文件输入只写预留契约，不写代码」（契约本身保留并转为正式，字段名见决策 2）

## 背景

ADR 0067 只为文件输入写了预留契约：`FileAttachmentV1` / `FilePart` 形状、`"file"` 能力门控、策略、脱敏与四家 provider
映射，理由是「无人产出、各 provider 全拒绝」的类型是死代码，新 part 进入 content union 必须与所有 provider 分支一起落地。
现在有了真实输入路径（`UserMessage.attachments`）与 provider 映射的完整需求，把预留契约转为正式实现。

## 决策

1. **与图片输入同构，而非另起一套。** `FileAttachmentV1`（持久化）/ `FilePart`（请求内）/ `FileInputPolicy`（业务开闸，默认关）
   与图片三件套一一对应；同一 `UserMessage.attachments` 入口按 `kind` 区分，parts 按提交顺序交错。入队准入与每轮 prompt 重建
   共用 `loop/attachment_parts.user_attachment_parts`（同一能力门 + admission），canonical base64 解码抽到
   `llm/attachment_codec.py` 由两类附件共用。
2. **字段名对齐 `ImageAttachmentV1`（`content` / `size` / `encoding`），不沿用预留稿的 `data` / `size_bytes`。** 两类附件与严格
   Journal `AttachmentV1` 字段同名，解码、字节估算、审计 descriptor 遍历都无需逐类特判。
3. **能力显式声明。** `ModelCapabilities.input_modalities` 增加 `"file"`（默认不含）；OpenAI Chat / Responses、Codex、Anthropic、
   Gemini 显式声明，OpenAICompat / DeepSeek / LiteLLM 保持 text-only。provider 序列化前的门控抽到 `providers/_modality_gate`，对图片
   与文件都生效；文件只允许出现在 user 消息，工具输出里的 `FilePart` 显式拒绝。
4. **成本按页给保守上界。** 策略启用时 admission 顺带数出 PDF 页对象，估 `页数 × page_token_ceiling`；数不到（页对象在压缩
   object stream 里）或策略未启用时取 `unknown_file_token_ceiling`。不解析 PDF 内容流、不引第三方库；首轮采样后由实测 usage 校准。
5. **strict audit 模式显式拒绝文件。** 严格 Journal 的 `AttachmentV1` 只有图片形状（无 `filename`、有 `detail`）；在 acceptance 前以
   `unsupported_modality` 落 durable `submission_rejected`，而不是靠形状校验偶然拒掉。扩 Journal schema 另议。
6. **新符号进实验层**：`taifeng.experimental` 导出 `FileAttachmentV1`、`FileInputPolicy`、`FilePart`，不进顶层 `__all__`。
7. **engine.py / pool.py 行数不增**：legacy 入队准入下沉 `attachment_parts.admit_user_attachments`，腾出的行数抵掉新增参数透传。

## 否决的方案

- **LiteLLM 声明 `"file"` 并透传 OpenAI `file` part**：litellm 1.86 能把该形状翻译给 Anthropic / Gemini / Bedrock 等，但能否接受取决于
  model 前缀路由到的后端；声明能力等于按模型名猜，违背「能力一律显式、不据模型名推断」。改为序列化前显式拒绝（同时修掉把 pydantic
  part 原样交给 litellm 的旧路径）。
- **按文件大小推页数**：扫描件单页数百 KB、文本页数 KB，任何「字节/页」常数都会在一类文档上偏差一个数量级。
- **引入 PDF 解析库做精确页数 / 文本抽取**：内核只做结构校验与保守估算，理解交给模型；第三方解析器扩大攻击面与依赖面。
- **能力不足时降级为「[附件：xxx.pdf]」文本**：用户明确给的输入被吞，与图片 user 侧「抛错不降级」的既有语义冲突。
- **扩 strict Journal `AttachmentV1` 以承载文件**：持久化 schema 变更（canonical hash、回放兼容）应独立评审，不随本切片夹带。

## 验证

CI（全部 Sim / mock transport）：`tests/llm/test_file_input.py`（形状、admission 边界、PDF 结构、估算）、`test_attachment_codec.py`、
`test_file_input_wire.py`（Chat / Responses / Codex / Anthropic / Gemini wire，OpenAICompat / LiteLLM 拒绝，能力声明，Sim 描述）、
`test_file_input_redaction.py`（capture + strict manifest）、`test_modality_gate.py`、`tests/loop/test_attachment_parts.py`、
`tests/loop/test_file_input_wiring.py`（真实 EnginePool + SimClient 端到端、策略 / 能力拒绝不落史、顺序、冷恢复与 JSONL 往返、
估算、子 runner 继承、strict audit 拒绝）、`tests/context/test_file_input_accounting.py`（估算与压缩占位）。
真实 LLM：`examples/real_llm/test_codex_file_input.py`（codex，场景 `codex_file_input`，随机核对码 PDF 逐字读出 + 脱敏）3/3 PASS；
其余 provider 的文件 wire 未做真实验证。
