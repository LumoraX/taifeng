## ADDED Requirements

### Requirement: 工具结果附件落引用
配置了附件存储时，工具返回的图片附件 SHALL 在 admission 通过后写入存储，`function_call_output` 条目 SHALL 只落引用形态。`ToolResult.attachments` SHALL 同时接受 `ImageAttachmentV1` 与 `ImageAttachmentRefV1`。

#### Scenario: 工具返回内联图片
- **WHEN** 工具返回一张 `ImageAttachmentV1`，pool 配置了存储
- **THEN** transcript 里该 output 的附件 SHALL 是引用形态，下一次采样发给 provider 的图片 SHALL 与工具返回的字节一致

#### Scenario: 工具返回引用
- **WHEN** 工具返回一个已在存储中的 `ImageAttachmentRefV1`
- **THEN** SHALL NOT 再次写入存储，条目里 SHALL 落同一引用

#### Scenario: 未配置存储时工具返回引用
- **WHEN** 工具返回引用形态而 pool 未配置存储
- **THEN** 该次调用 SHALL 以 `tool_attachment_rejected` 判错

### Requirement: `file_read` 读取附件引用
`make_file_read_tool(attach_images=True, attachment_store=...)` SHALL 把形如 `attachment:<ref>` 的 `path` 识别为附件引用：校验该引用出现在调用方 thread 的 transcript 中，然后把它作为图片附件返回，SHALL NOT 复制字节。引用不属于调用方 thread、不存在、或不是图片时 SHALL 返回明确的错误。

#### Scenario: 读回被驱逐的图片
- **WHEN** 模型以驱逐描述里的 `attachment:<ref>` 调用 `file_read`
- **THEN** 工具结果 SHALL 带该引用的图片附件，模型在下一次采样 SHALL 收到与原件一致的图片

#### Scenario: 引用不属于本 thread
- **WHEN** 以另一个 thread 的引用调用 `file_read`
- **THEN** SHALL 返回 `attachment_not_in_thread` 错误，SHALL NOT 返回附件

#### Scenario: 引用是文件而非图片
- **WHEN** 以 PDF 的引用调用 `file_read`
- **THEN** SHALL 返回 `attachment_not_image` 错误（工具结果不承载文件附件）

#### Scenario: 未配置存储
- **WHEN** `file_read` 未传 `attachment_store` 而 `path` 以 `attachment:` 开头
- **THEN** SHALL 返回 `attachment_store_not_configured` 错误

## MODIFIED Requirements

### Requirement: 能力边界
能力边界中的「strict SessionJournal 不支持」SHALL 更正为与实现一致：严格审计下工具图片附件随 `function_call_output` 对话项落账（完整正文），先过图片策略、再过审计 Session 的字节上限。

#### Scenario: 文档与实现一致
- **WHEN** 阅读 `tool-image-attachment.md` 的能力边界
- **THEN** 其对严格审计的描述 SHALL 与 `session-journal-business-integration.md` 及 `loop/audit_tool.py` 的行为一致
