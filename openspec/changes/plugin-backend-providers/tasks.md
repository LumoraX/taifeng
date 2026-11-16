# plugin-backend-providers 实施计划

> 定案（ADR 0145）：两段加载（方案 A），同步 `provide_backends` 只声明工厂；可提供后端为 `message_store` / `thread_directory` / `audit`（整份 `AuditConfig`——journal core 离不开 writer_id 与附件上限，不单独提供）。3.2 的审计用例落在 `tests/plugin/test_backend_capabilities.py::test_plugin_provided_audit_config`。

> 前置：`plugin-core`，且有具体宿主需求。分支 `feat/plugin-backend-providers`。

## 1. 先定时序
- [x] 1.1 在两段加载（A）与加载点前移（B）之间定案，写新 ADR，更新 `docs/architecture/capabilities/plugin-system.md` 的加载流程。Acceptance：ADR 标注对 0144 的修订关系；契约里的时序图与 `EnginePool.create` 的真实顺序一致。

## 2. 提供者
- [x] 2.1 `tests/plugin/test_backend_capabilities.py` 红测试：单个插件提供 `message_store` 后 pool 使用它；两个插件提供同一 capability 抛 `PluginCapabilityError`；宿主显式传参且插件也提供同样抛错；提供阶段结束后再提供被拒。Acceptance：观察红。
- [x] 2.2 实现 `ctx.backends.provide_message_store / provide_thread_directory / provide_journal_core` 与所选时序方案。Acceptance：测试绿；`pool.py` ≤ 800 行。
- [x] 2.3 回滚测试：提供阶段成功、后续 setup 失败时，已构建的 store 被关闭且不泄漏。Acceptance：测试绿。
- [x] 2.4 所有权测试：`pool.close()` 关闭插件提供的 store，且在插件 close 之后。Acceptance：测试绿。

## 3. 校验与审计
- [x] 3.1 插件提供的 store 走 `validate_injected_store`；不满足条件时报与显式注入相同的错误。Acceptance：测试绿。
- [x] 3.2 `tests/loop/test_audit_external_journal_core.py` 增加用例：审计会话使用插件提供的 `SessionJournalCore`，close 时 lease 释放；普通插件拿不到 lease。Acceptance：测试绿，既有 conformance 套件不变。
- [x] 3.3 恢复测试（R5）：用提供者插件写入、重建 pool 并加载同一插件后能恢复；评估并记录「漏加载提供者」时的表现。Acceptance：行为有测试且已入契约。

## 4. 验证
- [x] 4.1 更新 `docs/architecture/conversation.md`、`capabilities/plugin-system.md`、`docs/configurable-knobs.md`。
- [x] 4.2 `PYTHONPATH=src uv run pytest tests/ -q`、ruff 门禁、mypy strict。Acceptance：全绿。
- [x] 4.3 真实回归 `capability_matrix.py` 并提交台账。Acceptance：全部场景 PASS。
