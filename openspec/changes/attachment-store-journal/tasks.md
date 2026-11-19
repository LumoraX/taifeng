# attachment-store-journal 实施计划

> 前置：`attachment-store` 已合入并发布；有使用严格审计的宿主提出需求。分支 `feat/attachment-store-journal`。
> 开工前先完成第 1 节；其结论可能改变后续任务的形状。

## 1. 先定版本与兼容
- [ ] 1.1 写测试确认现有读取方（内置与外部 conformance 套件）读到未知判别值的附件记录时的行为。Acceptance：行为有测试记录。
- [ ] 1.2 在「递增 `schema_version`」与「session 级能力声明」之间定案，写 ADR，更新 `session-journal-core.md`。Acceptance：旧读取方的拒绝路径有测试，且错误与数据损坏可区分。
- [ ] 1.3 定案证据导出与留存责任（design 的 Open Questions 3、4），写入契约。

## 2. 记录形状
- [ ] 2.1 红测试：引用记录的校验、`payload_hash` 覆盖 `sha256`、离线验链不读存储、篡改摘要使验链失败。
- [ ] 2.2 实现 `AttachmentRefRecordV1` / `FileAttachmentRefRecordV1` 与按 `encoding` 的判别。Acceptance：未配置存储时既有 Journal 测试逐字节不变。

## 3. 写入
- [ ] 3.1 `audit_admission.py`：admission → `put` → 落引用记录；被拒绝的提交不调 `put`；字节上限按原件大小。Acceptance：测试绿。
- [ ] 3.2 `audit_tool.py`：工具图片附件落引用记录，`tool_outcome_committed` 的摘要不变。Acceptance：测试绿。
- [ ] 3.3 取消 `audit` 与 `attachment_store` 的互斥，更新 `attachment-store.md`。Acceptance：原互斥测试改为共存测试。

## 4. 回放与恢复
- [ ] 4.1 `replay_session.py`、`tool/replay.py`、`audit_resume.py` 经解析器取字节；内联录制与引用录制的摘要相等。Acceptance：互放测试绿。
- [ ] 4.2 回放时对象缺失 / 被篡改 → `ReplayDivergenceError`。Acceptance：测试绿。
- [ ] 4.3 在线降级留痕与其回放。Acceptance：测试绿。
- [ ] 4.4 时间线脱敏视图对引用记录的处理。Acceptance：`session-journal-timeline` 测试补用例。

## 5. 外部实现
- [ ] 5.1 扩充 `SessionJournalCore` conformance 套件，覆盖引用记录的保存与读取。Acceptance：内置实现通过；套件说明更新。

## 6. 验证
- [ ] 6.1 更新 `session-journal-core.md`、`session-journal-business-integration.md`、`session-journal-timeline.md`、`attachment-store.md`、`docs/configurable-knobs.md`、`docs/capability-matrix.md`。
- [ ] 6.2 全量 pytest、examples 冒烟、ruff 门禁、mypy strict。Acceptance：全绿。
- [ ] 6.3 真实回归 `capability_matrix.py` 并提交台账；`examples/real_llm/audit_smoke.py` 增加带图引用形态一例。Acceptance：通用能力全部 PASS。
