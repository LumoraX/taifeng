# ADR 0095：审计模式接受文件附件与工具结果里的图片

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §11](../architecture/capabilities/session-journal-business-integration.md)；
  [llm-file-input](../architecture/capabilities/llm-file-input.md)、
  [tool-image-attachment](../architecture/capabilities/tool-image-attachment.md)；ADR 0025

## 背景

审计模式下有两类附件进不来：

- 用户消息里的文件：Journal 的 `AttachmentV1` 只有图片的形状（没有文件名），带文件的消息在准入时被拒；
- 工具结果里的图片：落账代码假定「一个结果一条对话项」装不下图片，遇到附件就冻结整个 Session。

后一条的假定已经过时：`function_call_output` 对话项的 payload 早就有 `attachments` 字段。
请求落账时的脱敏也已经认识文件正文（`file_base64`）。缺的只是准入与落账这两处。

命中 ADR 0017 规则①。

## 决策

1. **文件附件另立 DTO（`FileAttachmentRecordV1`），不给 `AttachmentV1` 加字段**。Journal 的 canonical 序列化
   会写出全部字段，加一个可空的 `filename` 会让此后每条图片附件多出 `"filename": null`，
   图片附件的字节形状就变了。
2. **`submission_accepted.attachments` 按 `kind` 区分两种形状**。`kind == "file"` 走文件 DTO，
   其余走 `AttachmentV1`；判别函数显式给出，不依赖「哪个能校验通过就用哪个」。
3. **文件的准入与非审计路径同一口径**：同一个策略对象、同一套能力判断，
   另加审计 Session 自己的附件字节上限。不合格的消息 durable 拒绝，不保存正文。
4. **工具图片随 `function_call_output` 对话项落账，outcome record 只记摘要**。正文保存一份；
   摘要（类型、大小、SHA-256）让只读 outcome 的人知道结果带了什么、能与对话项对上。
5. **工具附件不合格时把结果变成错误，不冻结**。非审计路径就是这样处置的：模型看到拒绝原因，
   可以换个做法；调用与结果仍然成对。冻结是给「不知道发生了什么」准备的，这里知道。
6. **工具附件同样受 Session 的附件字节上限约束**。上限是给 Journal 体积设的，不因来源不同而不同。

## 替代方案

- **把文件正文外置，Journal 里只存引用**：ADR 0025 明确外置 blob 由宿主后端负责，内核的默认实现保存完整正文。
- **工具图片单独落一条对话项**：要新增对话项种类并约定它与结果的配对关系；既有字段已经够用。
- **工具附件不合格时冻结**：见决策 5。

## 后果

- 新增 DTO `FileAttachmentRecordV1`；`SubmissionAcceptedV1.attachments` 的元素类型变为两种形状之一。
- `prepare_user_message` 与 `audited_tool_batch` 各多一个策略参数，都有默认值。
- 行为变化：审计模式下工具返回图片不再冻结 Session；图片策略未启用时结果为错误。
- 行为变化：审计模式下带文件的用户消息在模型支持且策略启用时被接受。
- 未覆盖：PDF 以外的文件类型（随 llm-file-input 扩展）；工具结果里的文件附件（工具层尚无此能力）；
  来源标记（`origin`）在审计模式下仍不接受。
- R1：无业务概念。R2：不涉及。R3：沿用既有事件。R4：不涉及。R5：附件正文在 Journal 里，
  恢复后对话项完整。

## 验证

`tests/loop/test_audit_attachments.py`：文件被接受并落账、对话项形状与非审计路径相同、模型收到文件、
请求记录里的正文被脱敏、图片与文件同一条消息、恢复后附件仍在且续跑带着它、策略未启用或超限时
durable 拒绝、形状不合法时拒绝、工具图片到达模型与 Journal、策略未启用时结果为错误、
两种形状按 kind 区分、图片附件的形状不变。
`tests/loop/test_audit_tool.py`：附件随结果落账且 outcome 只记摘要、超过 Session 上限被拒、策略未启用被拒。
`tests/loop/test_file_input_wiring.py`：准入的拒绝与接受。
