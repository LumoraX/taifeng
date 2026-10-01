# ADR 0116：用户文件（PDF）输入晋升稳定层

- 状态：Accepted
- 日期：2026-09-30
- 关联：Amends #0068；[llm-file-input](../architecture/capabilities/llm-file-input.md)、
  [public-api](../architecture/public-api.md)；ADR 0066、0095、0110

## 背景

图片附件的 `ImageAttachmentV1` / `ImageInputPolicy` / `ImagePart` 在稳定层，同构的文件附件
`FileAttachmentV1` / `FileInputPolicy` / `FilePart` 停在实验层（ADR 0068）。稳定入口
`EnginePool.create(file_input_policy=)` 与 `UserMessage.attachments` 都要用到它们，下游只依赖稳定层时
无法把用户的 PDF 作为消息附件交给模型（平台对接缺口清单第 14 项）。

当初留在实验层的理由是真实验证只有 codex 一家，其余 provider 的文件 wire 只有单测。

## 决策

1. **`FileAttachmentV1` / `FileInputPolicy` / `FilePart` 晋升稳定层**，`llm-file-input` 契约转 ✅。
   实验层保留同名入口一个发布版本（访问时告警，ADR 0110 的做法）。
2. **晋升依据**：
   - codex：能力矩阵的 provider 专属场景 `codex_file_input` 持续通过（随机核对码读出 + 登记 + 脱敏）；
   - Gemini：`examples/real_llm/file_input_verify.py` 在 `gemini-3.1-pro-preview` 上 5 项通过——端点接受
     `inlineData`、模型逐字读出随机核对码、capture 与事件日志不含正文；
   - 审计模式已接受文件附件（ADR 0095），冷恢复、压缩占位、成本估算都有测试。
3. **验证边界如实保留在契约里**：OpenAI Chat / Responses 与 Anthropic 的文件 wire 仍只有单测，没有可用
   的 key。晋升承诺的是 canonical 形态、admission 规则与能力门控的兼容性，不是「每家端点都验过」。

## 不做

- **等四家 provider 都真实验证过再晋升**：OpenAI 官方与 Anthropic 的 key 长期不可得（图片输入的
  `openai_image_input` 场景同样是未执行状态，而图片类型早已在稳定层）；`file_input_verify.py` 对任何
  声明了 `"file"` 能力的 provider 都能跑，拿到 key 即可补验。
- **顺带把工具回传文件纳入契约**：入口仍限于 user 消息。

## 影响

- R1–R5：无行为变化，只改导出与契约状态。

## 验证

- `tests/test_public_api.py`：三个名字在稳定层、从实验层仍可取到并告警。
- 真实端点见决策 2；全量台账重跑见提交说明。
