## Context

来源：`attachment-store` 的 Decision 8（审计模式本期互斥）。**开工条件：`attachment-store` 已发布，且有使用严格审计的宿主提出体积或留存方面的需求。** 没有的话保持提案状态。

现状（已核对）：`AttachmentV1`（`conversation/journal/records.py`）与 `FileAttachmentRecordV1`（`attachment_records.py`）持有完整 `content`；`SubmissionAcceptedV1.attachments` 与 `_FunctionCallOutputItemPayload.attachments` 各存一份；`payload_hash(record.payload)`（`framing.py`）覆盖这些字节并进入 `record_hash`；`canonical_attempt_digest` 对**未脱敏**的请求求值，正文是 preimage 的一部分；`session-journal-core.md` 把「blob 外置」列为不提供。

## Goals / Non-Goals

目标：审计会话的 Journal 不再承载附件正文，同时不削弱三条已有保证——hash 链可验证、回放逐字节一致、被拒绝的提交不留正文。非目标见 proposal。

## Decisions

1. **记录里放引用，链上承诺摘要**。引用记录含 `sha256`；它在 `payload_hash` 的 preimage 里，所以篡改附件存储里的对象会在回放的完整性校验处暴露，篡改记录里的摘要会破坏 hash 链。验证一条记录本身不需要取附件字节。
2. **记录形状用新类型，不给旧类型加可选字段**。`AttachmentV1` 的 `content` 必填是已发布的契约；新增 `AttachmentRefRecordV1`，由 `encoding` 判别。`schema_version` 是否需要递增取决于旧读取方遇到未知判别值的行为——开工时先写测试确认（见 Open Questions）。
3. **回放经解析器取字节，摘要算法不动**。`canonical_attempt_digest` 的 preimage 仍是带正文的完整请求；引用只改变正文从哪里来。因此同一段对话无论录制时是内联还是引用，摘要相同，旧录制与新录制可以互相回放。
4. **被拒绝的提交仍不留正文**。admission 失败发生在 `put` 之前（`attachment-store` 已保证）；`submission_rejected` 的语义不变。
5. **字节上限按原件大小在 admission 执行**，与是否外置无关。
6. **取不回的附件在审计回放中是分叉，不是降级**。在线运行时（非回放）沿用 `attachment-store` 的降级规则并在 Journal 里记一条事实；回放时请求内容与录制不同即 `ReplayDivergenceError`——这正是回放要报告的信号。

## Open Questions（开工前必须先定）

1. **旧读取方的行为**：未升级的 journal 读取方读到引用记录会怎样——拒绝整个 session，还是只在该记录处失败？这决定是否递增 `schema_version` 以及是否需要一个 session 级的能力声明。
2. **外部 `SessionJournalCore` 实现**（ADR 0145 允许插件提供）：conformance 套件需要新增哪些用例，才能保证外部实现正确保存引用记录？
3. **审计证据的导出**：一份可独立验证的证据包是否要求把被引用的对象一并导出？若是，需要一个「按 session 收集全部引用」的只读接口（可复用 `collect_attachment_refs`）。
4. **留存与删除**：审计要求保留 N 年时，附件存储的对象谁负责不被回收？内核不做引用计数，这条约束须写进契约并由宿主承担。

## Risks

- 审计证据从一个文件变成「Journal + 存储对象」，备份或导出时漏掉后者会使历史附件不可回放。
- 记录形状变更若处理不当会让旧版本无法读取新 session；必须有显式的版本或能力信号，不能靠解析失败。
