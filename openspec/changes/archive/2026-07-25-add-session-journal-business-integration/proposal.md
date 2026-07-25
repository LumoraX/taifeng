## Why

Taifeng 已有 SessionJournal durable core，但普通 `UserMessage → LLM → Tool/call_skill → assistant`
执行路径仍只写 legacy transcript 和可丢失事件，无法证明用户已看到的内容、外部 effect 与最终结果均有
一致的可靠审计事实。现在需要交付首个严格纵向切片，把 ADR 0025 的唯一事实源和 fail-closed 语义真正
接入新 Session，同时保持未启用审计的现有行为不变。

## What Changes

- 为新建 Session 增加显式 `audit_required` 模式；Journal 初始化 durable ack 成功后才启动 Engine。
- 新增版本化 submission、turn、LLM、Tool、Skill、thread/session 与 `conversation_item` 领域记录。
- 将用户输入改为 durable acceptance-before-enqueue；领域 outcome 与对应对话项在同一个 Journal batch
  中提交，hot history 和 MessageStore 物化投影只消费 durable-acked 对话项。
- 在每次 LLM 网络 attempt 前提交 request intent，并在任何 UI delta 可见前提交 response checkpoint。
- 在 Tool/同步 `call_skill` effect 前提交 durable intent，effect 后提交确定 outcome；无法确认时记录
  `UNKNOWN` 并冻结当前 Session，禁止自动重复非幂等 effect。
- 增加单 Session audit coordinator、能力门禁、目标 turn 取消、幂等生命周期终结与 per-session
  Journal writer 释放；Journal 故障不影响其他 Session。
- audit-required 模式暂不支持旧 Session resume、HITL/审批、suspend、compaction/rewind、memory、
  instruction 更新、hooks、orchestration、detached spawn 或 peer；这些路径在 effect 前稳定拒绝。
- 保持未启用 Journal 的 EnginePool、AgentEngine、MessageStore、EventMsg 与业务 skill 调用行为兼容。

## Capabilities

### New Capabilities

- `session-journal-business-integration`: 新 Session 普通业务主链的 Journal-first submission、LLM/Tool/
  `call_skill` intent/outcome、durable conversation projection、能力门禁与 per-Session fail-closed 契约。

### Modified Capabilities

无。当前仓库没有已归档到 `openspec/specs/` 的主规格；本能力建立在尚未归档的
`add-session-journal-durable-core` 实验性能力之上，不改变 legacy MessageStore 的默认契约。

## Impact

- 主要涉及 `src/taifeng/conversation/journal/`、`src/taifeng/loop/`、`src/taifeng/llm/`、
  `src/taifeng/tool/` 和同步 `call_skill` 路径，并新增对应 contract/integration tests。
- `JsonlSessionJournalCore` 增加 lease-safe `close_session()`；Journal core 仍由调用方拥有，EnginePool
  不关闭全局 core。
- 默认 JSONL transcript 在审计模式下成为可删除、可从 Journal 重建的物化投影，不是第二事实源。
- 新增 LLM attempt observer 和 ToolSpec audit metadata；缺少这些能力的 provider/tool 不能进入严格模式。
- 需要同步 conversation、agent-loop、llm-client 活文档与能力矩阵；基础层变更合入前必须完成全量测试、
  Sim selfcheck、真实 LLM capability matrix，并刷新两份 real-LLM ledger。
- 不迁移历史 Session，不提供跨进程 recovery/open/unfreeze，也不在本变更中 archive 或 merge。
