# SessionJournal Timeline 与迁移能力契约

> 状态：Experimental。关联 ADR 0025（Phase 5）、ADR 0104。依赖 `session-journal-core` 与
> `session-journal-business-integration`。

## 1. 范围

Journal 之上的四件只读 / 运维能力：

| 能力 | 入口 | 写 Journal |
| --- | --- | --- |
| Timeline 投影 | `JournalTimelineProjector.read` | 否 |
| 脱敏视图 | `redact_payload`，`read(view=)` | 否 |
| 旧 transcript 导入 | `import_legacy_transcript` | 是：新建 Session 的初始化批次与导入批次 |
| 投影重建 | `rebuild_projections` | 否 |

Timeline 从 Journal 投影，不从 EventMsg 反推；EventMsg 仍是可丢失的实时通知，丢失后凭 `after_seq` 补读。

## 2. Timeline 投影

`TimelineItem`——一条记录的稳定视图：

| 字段 | 含义 |
| --- | --- |
| `seq` / `record_id` | 回链原记录 |
| `record_type` | 领域记录类型；不另造语义事件 |
| `recorded_at` / `occurred_at` | 落账时间 / 发生时间（缺失为空）；展示以 `recorded_at` 为准 |
| `actor_kind` / `actor_source` | 触发者 |
| `thread_id` / `submission_id` / `turn_id` / `operation_id` / `causation_id` | 归属与因果 |
| `call_id` | payload 顶层（或对话项 payload 内）的 `call_id` / `parent_call_id` / `related_call_id` |
| `skill_id` | 同上取 `skill_id` / `then_skill_id` / `entry_skill_id` / `target_skill_id` |
| `payload` | 按视图给出（见 §3） |
| `payload_hash` | 原 payload 的 canonical hash，三种视图相同 |
| `redactions` | `redacted` 视图的脱敏清单 |

`TimelineFilter`：`thread_id` / `turn_id` / `submission_id` / `actor_kind` / `record_types` / `call_id` /
`skill_id`，为空的维度不限制。

`read(session_id, after_seq=0, view="full", filter=None, limit=None) -> TimelinePage`：

- 条目按 `seq` 升序；`last_seq` 是本次扫过的最后一条记录的序号（含被筛掉的），下一次以它作
  `after_seq` 接力，不漏不重；
- `limit` 到达时停在最后一个返回条目；
- `after_seq` 为负或 `limit` 非正抛 `ValueError`；
- `audit_complete` 在 `metadata_only` 视图为 False。

## 3. 脱敏视图

| 视图 | payload | 其他 |
| --- | --- | --- |
| `full` | 原 payload | — |
| `redacted` | 正文字段换成 `{"redacted": true, "sha256", "length"}` 占位，容器结构保留 | `redactions` 清单：每项路径、摘要、长度 |
| `metadata_only` | 空 | `audit_complete = False` |

脱敏按字段名而不是按内容：承载正文的字段是有限的一组（用户与模型的文本、工具参数与结果、实发请求
与回复、附件正文、人的答复、peer 消息、定义快照……），在任何层级出现都脱去。同一 payload 永远得到
同一结果；脱敏只发生在视图，唯一事实源不动。

## 4. 旧 transcript 导入

`import_legacy_transcript(journal_core, store, session_id, thread_id, writer_id, max_attachment_bytes,
max_total_attachment_bytes) -> LegacyImportResult`

- 旧文件 `<threads_dir>/<thread_id>.jsonl` 必须存在、首行元数据完整、thread id 相符、不是审计投影；
  损坏的行让导入失败（`legacy_transcript_line_invalid`），不静默跳过。
- 新建 Session：root thread 就是旧 thread id，`config.history_status = legacy_unverified`。
- 导入批次：`legacy_import`（`LegacyImportV1`：文件名、文件 SHA-256、行数、导入数、跳过的条目）
  领头，其后是旧对话项（原样、保序，`metadata.legacy_source_line` 记来源行号，`source_record_id`
  指向 `legacy_import`）。对话项集合与 `load_history` 一致（只含 bare 条目与完整提交的原子 batch）。
- Journal 不认识的条目（`spawn` 等运行态锚点）不导入，记在 `skipped`（行号、kind）。
- 旧文件原样搬到 `<threads_dir>/legacy/`；投影按 Journal 重建，带 audited marker 与 `history_status`。
- 完成后 lease 释放，Session 未终结：`get_or_create(session_id, entry_skill_id, resume_thread_id=thread_id)`
  可以接管，history 是导入的对话项。
- 未导入的旧 thread 在审计模式下不能接管（`audit_resume_marker_missing`，既有行为）。

错误 `LegacyImportError.code`：`legacy_transcript_missing` / `legacy_transcript_empty` /
`legacy_transcript_meta_missing` / `legacy_transcript_thread_mismatch` / `legacy_transcript_already_audited` /
`legacy_transcript_line_invalid`。

## 5. 投影重建

`rebuild_projections(journal, store, session_id) -> ProjectionRebuildResult`

- 逐 thread：`thread_created` 记录给出 thread 与它的入口 skill、来源、extra；`conversation_item` 记录按
  序号给出内容。
- 没有投影的 thread 新建（带 audited marker）；已有投影是 Journal 内容的前缀时补齐后缀；分叉的投影
  不改写，`status = divergent`，由调用方删除后重跑。
- 幂等：重跑不改变已一致的投影。
- 删除全部物化数据后重建，接管的结果与删除前相同。

## 6. R1–R5 影响

- R1：无业务概念。
- R2 / R3 / R4：只读，不触发压缩、不发事件、不引入等待。
- R5：投影可从 Journal 重建；旧 thread 可导入后接管。

## 7. 验收

Timeline 按序映射全部记录并回链、用户输入完整可读；按 call / type / actor / turn 筛选；`limit` 与
`after_seq` 接力不漏不重；脱敏确定性且保留结构、三视图的 `payload_hash` 相同、`metadata_only`
显式不完整；导入后 Journal 健康、投影带 marker、可接管并接着对话、跳过的锚点有记录、缺失 / 损坏 /
未导入各有稳定拒绝；删除投影后重建逐字相同、幂等、可接管；分叉的投影只报告不改写。
