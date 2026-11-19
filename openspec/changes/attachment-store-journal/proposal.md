## Why

`attachment-store` 把附件字节移出了 JSONL transcript，但严格审计（SessionJournal）路径被显式排除：`audit=` 与 `attachment_store=` 不能同时配置。审计会话恰恰是最在意体积与留存的场景——今天附件的完整 base64 在 Journal 里出现两次（`submission_accepted` 与 `conversation_item`），且每条记录的 `payload_hash` 都要对这些字节求值。

## What Changes

- Journal 的附件记录新增引用形态：`AttachmentRefRecordV1` / `FileAttachmentRefRecordV1`，含 `ref`、`media_type`、`size`、`sha256` 与已校验元数据，不含正文。
- `payload_hash` 覆盖引用记录（含 `sha256`），hash 链因此仍对附件**内容**作出承诺，但不再对字节本身求值。
- 取消 `audit` 与 `attachment_store` 的互斥；审计模式下配置了存储时，新写入的记录用引用形态。
- 审计的字节上限（`max_attachment_bytes` / `max_total_attachment_bytes`）继续在 admission 时按原件大小执行。
- 回放（`replay_session`、`JournalReplayClient`、工具回放）经同一个解析器取回字节；`canonical_attempt_digest` 的 preimage 不变。
- 时间线脱敏视图对引用记录无需替换正文，只保留已有的摘要占位语义。

## Capabilities

### New Capabilities

- `attachment-store-journal`：严格审计下的附件引用记录、hash 链承诺与回放。

### Modified Capabilities

- `session-journal-core`：「blob 外置」从不提供改为经 `AttachmentStore` 提供。
- `session-journal-business-integration`：附件「只接受完整 inline base64」放宽为内联或引用。
- `attachment-store`：移除与 `audit` 的互斥。

## Non-goals

不改既有 Journal 记录（追加写）；不迁移已落账的内联附件；不改变 lease、writer epoch 与恢复状态机；不把附件存储纳入 Journal 的事务——两者的一致性靠「先存后记」与回放时的完整性校验保证。

## R1–R5 影响

- R1：无业务概念。
- R2：不涉及（请求字节不变）。
- R3：审计下同样发 `attachment_stored` / `attachment_unavailable`；另需决定取不回的附件在审计回放中是降级还是判为回放分叉（见 design）。
- R4：存取可取消。
- R5：**直接相关**。Journal 是审计真相源；引用化后，一份完整的审计证据 = Journal + 附件存储中被引用的对象。

## Impact

实现：`src/taifeng/conversation/journal/{records,attachment_records,item_records,redaction}.py`、`src/taifeng/loop/{audit_admission,audit_tool,audit_resume,replay_session}.py`、`src/taifeng/tool/replay.py`、`src/taifeng/llm/providers/{replay,replay_match}.py`、`src/taifeng/llm/audit_redaction.py`。

测试：`tests/conversation/journal/`、`tests/loop/test_audit_*`、`tests/llm/test_journal_replay_*`、外部 journal core 的 conformance 套件。

文档：新 ADR、`session-journal-core.md`、`session-journal-business-integration.md`、`session-journal-timeline.md`、`attachment-store.md`。

前置：`attachment-store` 已合入并经过至少一个发布周期。触及 `conversation/` 与 `loop/`，合入前须全量真实回归。
