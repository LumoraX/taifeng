# ADR 0142：SessionJournal audit-required 集成的发布边界

- 状态：Accepted
- 日期：2026-11-14
- 关联：[SessionJournal business integration 能力契约](../architecture/capabilities/session-journal-business-integration.md)、[ADR 0114](0114-session-journal-backend-seam.md)

## 背景

SessionJournal 已经接入 `EnginePool` 的 audit-required Session：Journal-first durable admission、LLM/tool/skill
记录、spawn/join/peer、挂起与 Resume、writer takeover、Timeline、脱敏、旧 transcript 导入、投影重建和离线 replay
均有引擎级测试；`examples/real_llm/audit_smoke.py` 也有 Codex 真实验证记录。但 durable core 和入口仍按实验层发布，
能力矩阵中 business integration 继续标成 🧪，与架构文档和实际门禁不一致。

## 决策

1. 将 **audit-required Session business integration** 登记为“Supported bounded scope”，并在能力矩阵标记 ✅。
2. 该晋升只覆盖契约列出的能力；`AuditCapabilityError` 的拒绝面是发布门禁，不能通过配置绕过。
3. `conversation.journal` durable core 继续保持实验能力；`taifeng.experimental` 中的入口继续保持实验 API 层级。
4. 不把本次晋升解释为 provider 策略的真实 LLM 全覆盖；真实 smoke 只证明 Journal 集成和 Codex 记录/恢复路径。

## 验证门

- `tests/loop/test_audit_config.py`：静态能力门禁与工具 metadata fail-closed。
- `tests/loop/test_audit_engine_turn_e2e.py`、`test_audit_spawn.py`、`test_audit_peer.py`、`test_audit_phase5.py`：引擎级 durable lineage、接管和 Timeline。
- `tests/loop/test_journal_replay_session.py`：离线 replay 不分叉。
- `examples/real_llm/audit_smoke.py`：Codex 真实 smoke 记录 24/24 checks，strict verify HEALTHY。

不在上述门内的能力保持明确拒绝或实验状态，不得因该行标记而获得默认启用语义。
