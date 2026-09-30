# ADR 0104：Journal Phase 5——Timeline 投影、脱敏、旧 transcript 导入与投影重建

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-timeline](../architecture/capabilities/session-journal-timeline.md)；ADR 0025（Phase 5）、0053

## 背景

ADR 0025 把 Journal 分五个阶段落地，前四个阶段已完成：durable core、恢复与冻结、Session/Thread/
Submission 接入、effect 边界接入。第五阶段是「Timeline 与迁移」：审计者怎么读 Journal、敏感内容怎么在
视图里脱去、审计之前的 thread 怎么进入审计、投影丢了怎么重建。

## 决策

1. **Timeline 直接映射领域记录**，每条记录一个条目，回链 `record_id` / `seq`，展示 `recorded_at`。
   不另造语义事件：领域记录已经是唯一权威表示，再造一层就有两份需要维护一致的事实。
2. **筛选维度取自记录本身**：thread、turn、submission、actor、record type，外加从 payload 里按固定字段名
   取出的 `call_id` 与 `skill_id`。不做全文检索，不解释 payload 语义。
3. **`after_seq` 接力**。`last_seq` 是扫过的最后一条记录的序号（含被筛掉的），客户端断线后从它继续，
   不漏不重。实时 EventMsg 只是「新水位到了」的通知。
4. **脱敏按字段名而不是按内容**。承载正文的字段是有限的一组，逐个列出；命中的整值换成同样形状的占位
   （摘要、长度），容器结构保留，附清单与原 payload hash。按内容猜（长度、字符类别）不确定，
   而不确定的脱敏没有意义。
5. **三种视图**：`full`、`redacted`、`metadata_only`；后者显式 `audit_complete = False`。脱敏只发生在视图，
   唯一事实源不动。
6. **旧 transcript 导入成一个新 Session**，root thread 就是旧 thread id，历史标 `legacy_unverified`：
   Journal 保证的是「从导入这一刻起」的完整性，导入之前发生过什么只能相信旧文件。导入记下旧文件的摘要、
   行数与逐条来源行号；旧文件原样留档；损坏的行让导入失败而不是跳过——旧文件是这段历史唯一的依据。
7. **Journal 不认识的条目不导入，但记下来**。`spawn` 等运行态锚点在审计模式下由记录承担（ADR 0098），
   导入的 Session 里没有这些运行态；跳过的条目在 `legacy_import` 里逐条可查。
8. **投影重建只补不改**。已有投影是 Journal 的前缀就补齐后缀；分叉的只报告，由人删除后重跑。
   重建代码不应该有「覆盖一份看起来不对的文件」的权力。

## 不做

- 全文检索、聚合统计：读 Journal 的工具应该建在 Timeline 之上，不进内核。
- 导出格式（CSV / 外部系统）：宿主自定。
- 加密、WORM、外置 blob：ADR 0025 已列为范围外。

## 影响

- R1：无业务概念。
- R2–R4：只读能力，不触发压缩、不发事件、不引入等待。
- R5：投影可从 Journal 重建；旧 thread 可导入后接管。

### 行为变化

- 新增实验层入口：`JournalTimelineProjector` / `TimelineFilter` / `TimelineItem` / `TimelinePage`、
  `redact_payload` / `RedactedPayload`、`import_legacy_transcript` / `LegacyImportResult` / `LegacyImportError`、
  `rebuild_projections` / `ProjectionRebuildResult`；`JsonlMessageStore.threads_dir` 公开。
- 既有行为无变化。

## 验证

`tests/loop/test_audit_phase5.py`（8 项）。全量 `pytest tests/` 通过；ruff 门禁与 `mypy src/` 清零。
ADR 0025 Phase 5 的最后一项「全量真实 LLM 回归」由负责人在集成时执行（W7.3）。
