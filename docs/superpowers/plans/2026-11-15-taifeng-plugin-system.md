# Taifeng Kernel Plugin System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Taifeng 增加一个受内核生命周期、作用域、取消和可观测性约束的插件扩展层，让业务系统实现插件包而不是修改 Taifeng 内核。

**Architecture:** Taifeng 提供显式注入的 `PluginManager`、`PluginContext` 和可逆 `PluginHandle`。第一阶段只支持 `EnginePool` 级插件，插件通过受限注册 API 接入现有 ToolRegistry、HookRunner、TelemetrySink、SkillSource 和存储后端；插件不能直接拿到 actor、prompt、history 或业务对象。第二阶段再增加 per-engine/per-session scope 和可选插件发现，保持默认启动路径与当前行为完全一致。

**Tech Stack:** Python 3.12+, anyio, frozen dataclass / Protocol, Pydantic EventMsg, pytest asyncio, existing EnginePool/registry/Telemetry/SessionJournal seams。

---

## 设计边界

### 内核必须提供

- 插件身份、版本、依赖和作用域声明。
- 依赖拓扑排序、重复 ID 检查、循环依赖拒绝。
- setup 失败时的反向回滚；close 时按逆依赖顺序撤销注册。
- `CancellationToken` 传播、异步 setup/close 和有界 close 超时。
- 每个注册动作返回可逆 `RegistrationHandle`，避免插件卸载后残留工具、hook 或 sink。
- `plugin_loaded`、`plugin_failed`、`plugin_unloaded` EventMsg，包含 plugin_id、version、scope、duration_ms 和稳定 failure_class，不包含业务正文。
- capability allowlist：插件只能调用显式注入的注册方法，不能访问 `AgentEngine` 私有 actor、`TurnRunner`、prompt/history 或 SessionJournal 原始 lease。

### 业务层必须提供

- 业务工具、业务 skill、业务 prompt、ERP/CRM/财务/企业微信/飞书连接器。
- 业务权限策略、业务审批文本、业务 UI、外部 API 凭据和数据转换。
- 具体的 `MessageStore`、`ThreadDirectory`、`IndexHook`、`SessionJournalCore` 实现。

### 本计划明确不做

- 不复制 Cordis 的全局插件图、TypeScript declaration merging、profile/bundle/HMR 体系。
- 不默认扫描 Python entry points 或隐式 import 插件；第一版只接受 `EnginePool.create(plugins=...)` 显式注入。
- 不把 `tenant_id`、宿主业务名、业务审批术语或具体 UI 放入 `src/taifeng/plugin/`。
- 不允许插件在 turn 中途直接改 prompt/history；所有变更必须通过现有 registry/context/compression 协议。
- 不在第一版实现热卸载正在运行的 tool；卸载先拒绝 active registration，等运行中的调用收敛后再释放。

## 文件结构和职责

- Create: `src/taifeng/plugin/errors.py` — 重复、缺依赖、循环、setup/close 失败和 scope 错误。
- Create: `src/taifeng/plugin/types.py` — `PluginSpec`、`Plugin`、`PluginHandle`、`RegistrationHandle`、`PluginScope`。
- Create: `src/taifeng/plugin/context.py` — 受限 `PluginContext` 和 capability-specific registration methods。
- Create: `src/taifeng/plugin/manager.py` — topological load、rollback、reverse close、timeout 和状态快照。
- Create: `src/taifeng/plugin/__init__.py` — stable internal package exports；实验入口从 `taifeng.experimental` 导出。
- Modify: `src/taifeng/loop/pool.py` — 在 registry/store/hook 初始化后、engine 创建前加载显式 plugins；pool close 时关闭 manager。
- Modify: `src/taifeng/tool/registry.py` — 将现有 register/unregister 包装为可逆 registration handle，保留当前直接 API。
- Modify: `src/taifeng/telemetry/sink.py` — 增加多 sink 的 registration handle，不改变现有 attach API。
- Modify: `src/taifeng/loop/event.py` / `src/taifeng/loop/event_turn.py` — 增加三个插件生命周期事件并加入 Msg union。
- Modify: `src/taifeng/experimental.py` — 导出 `Plugin`, `PluginSpec`, `PluginContext`, `PluginManager`。
- Create: `tests/plugin/test_manager.py` — manager 生命周期、依赖、rollback、cancel、timeout。
- Create: `tests/plugin/test_context.py` — capability allowlist、registration handle、scope 隔离。
- Create: `tests/loop/test_pool_plugins.py` — EnginePool 集成和 shutdown 行为。
- Create: `tests/telemetry/test_plugin_events.py` — console/jsonl/OTel 生命周期事件。
- Create: `examples/plugin_system/demo.py` — 一个无业务名的 `RedactedAuditSinkPlugin` 和 `ReadOnlyToolPlugin` 示例。
- Create: `docs/architecture/capabilities/plugin-system.md` — 数据结构、生命周期、事件、限制和验证契约。
- Create: `docs/decisions/0143-plugin-lifecycle-and-scope.md` — 为什么采用显式插件与 capability allowlist。
- Modify: `docs/architecture/agent-loop.md`, `docs/architecture/conversation.md`, `docs/capability-matrix.md`, `docs/README.md` — 同步现状和能力登记。

### Task 1: 定义插件契约和失败边界

**Files:**
- Create: `src/taifeng/plugin/errors.py`
- Create: `src/taifeng/plugin/types.py`
- Create: `src/taifeng/plugin/context.py`
- Create: `docs/architecture/capabilities/plugin-system.md`
- Create: `docs/decisions/0143-plugin-lifecycle-and-scope.md`
- Test: `tests/plugin/test_manager.py`
- Test: `tests/plugin/test_context.py`

- [ ] **Step 1: 写失败测试，固定最小 API**

```python
class DemoPlugin:
    spec = PluginSpec(id="demo", version="1", requires=(), scope="pool")

    async def setup(self, ctx: PluginContext) -> PluginHandle:
        return ctx.register("demo-capability", object())

    async def close(self) -> None:
        pass


async def test_plugin_spec_is_frozen_and_registration_is_reversible():
    manager = PluginManager()
    await manager.load((DemoPlugin(),))
    assert manager.snapshot().loaded_ids == ("demo",)
    await manager.close()
    assert manager.snapshot().loaded_ids == ()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=src uv run pytest tests/plugin/test_manager.py::test_plugin_spec_is_frozen_and_registration_is_reversible -q`

Expected: FAIL because `PluginSpec`, `PluginContext` and `PluginManager` do not exist.

- [ ] **Step 3: 实现最小类型和错误集合**

`PluginSpec` 使用 `@dataclass(frozen=True, slots=True)`，字段为 `id: str`、`version: str`、`requires: tuple[str, ...]`、`scope: Literal["pool"]`。`Plugin` 是带 `spec` 和异步 `setup(ctx)` 的 Protocol；`PluginHandle` 和 `RegistrationHandle` 都只有幂等的 `async close()`。`PluginContext` 第一版只保存 manager 的私有 capability registry，不暴露 engine、runner 或 history。

错误类固定为 `DuplicatePluginError`、`MissingPluginDependencyError`、`PluginDependencyCycleError`、`PluginSetupError`、`PluginCloseError`、`PluginCapabilityError` 和 `PluginActiveRegistrationError`，错误消息只包含 plugin id、capability 名和稳定 code。

- [ ] **Step 4: 运行 focused tests**

Run: `PYTHONPATH=src uv run pytest tests/plugin/test_manager.py tests/plugin/test_context.py -q`

Expected: PASS for frozen spec, duplicate ID, missing dependency, cycle detection, context capability denial, idempotent close。

- [ ] **Step 5: 提交**

```bash
git add src/taifeng/plugin tests/plugin docs/architecture/capabilities/plugin-system.md docs/decisions/0143-plugin-lifecycle-and-scope.md
git commit -m "feat: define taifeng plugin contract"
```

### Task 2: 实现依赖加载、rollback 和 shutdown

**Files:**
- Modify: `src/taifeng/plugin/manager.py`
- Modify: `src/taifeng/plugin/context.py`
- Test: `tests/plugin/test_manager.py`

- [ ] **Step 1: 写失败测试，覆盖实际顺序**

```python
async def test_dependency_order_and_reverse_close():
    events: list[str] = []
    manager = PluginManager()
    await manager.load((PluginB(events), PluginA(events)))
    assert events == ["setup:a", "setup:b"]
    await manager.close()
    assert events == ["setup:a", "setup:b", "close:b", "close:a"]


async def test_setup_failure_rolls_back_prior_plugins():
    events: list[str] = []
    with pytest.raises(PluginSetupError, match="bad"):
        await PluginManager().load((PluginA(events), FailingPlugin(events)))
    assert events == ["setup:a", "setup:bad", "close:a"]
```

- [ ] **Step 2: 运行失败测试**

Run: `PYTHONPATH=src uv run pytest tests/plugin/test_manager.py::test_dependency_order_and_reverse_close tests/plugin/test_manager.py::test_setup_failure_rolls_back_prior_plugins -q`

Expected: FAIL because dependency sorting and rollback are absent.

- [ ] **Step 3: 实现拓扑加载和有界关闭**

`PluginManager.load()` 先按 plugin id 建表，再用 Kahn 算法排序；发现重复、缺依赖或剩余入度时抛稳定错误。每个 setup 成功后立即记录 handle；后续失败时按已成功顺序反向 close，原始 setup error 保留为首因，rollback error 只进入日志和 `plugin_failed` 的 `rollback_errors` 数量。`close(timeout_seconds=5.0)` 用 anyio cancellation scope 反向关闭所有 plugin；正在运行的 registration 不允许被静默拔除，返回 `PluginActiveRegistrationError`。

- [ ] **Step 4: 增加 cancel/timeout/重复 close 边界**

测试 `CancellationToken` 在 setup 中取消、close 超时后 manager 进入 `closed_with_errors`、重复 close 幂等、setup 完成后 plugin 再次注册 capability 被拒绝。

- [ ] **Step 5: 运行 focused tests 并提交**

Run: `PYTHONPATH=src uv run pytest tests/plugin/test_manager.py -q`

```bash
git add src/taifeng/plugin/manager.py src/taifeng/plugin/context.py tests/plugin/test_manager.py
git commit -m "feat: add bounded plugin lifecycle manager"
```

### Task 3: 把插件接入 EnginePool，不改变默认路径

**Files:**
- Modify: `src/taifeng/loop/pool.py`
- Modify: `src/taifeng/experimental.py`
- Test: `tests/loop/test_pool_plugins.py`

- [ ] **Step 1: 写 EnginePool 集成失败测试**

```python
async def test_pool_loads_plugins_before_first_engine(tmp_path, skills_dir):
    plugin = RecordingToolPlugin()
    pool = await EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "threads",
        model_client=_sim_client(),
        compressors=[],
        plugins=(plugin,),
    )
    engine = await pool.get_or_create(session_id="plugin-test", entry_skill_id="entry")
    assert plugin.setup_completed_before_engine_creation
    assert engine.tool_registry.get("recording_tool") is not None
    await pool.close()
    assert plugin.closed
```

- [ ] **Step 2: 实现显式 `plugins=` 注入**

给 `EnginePool.create()` 增加 `plugins: Sequence[Plugin] = ()`，默认空元组。初始化顺序固定为：resolve dependencies → create core registries/store/directory/hook runner → `PluginManager.load()` → publish pool → create engine。任何 plugin setup 失败都不得发布半初始化 EnginePool；pool close 先停止新 submission，再等待 active work，最后关闭 PluginManager。

- [ ] **Step 3: 保留直接 API 和默认行为**

未传 `plugins` 时，不创建业务插件对象、不改变工具版本号、不新增事件、不改变默认 JSONL/SQLite 路径。已有 `extra_tools`、`sink`、`message_store`、`audit` 参数继续优先于插件注册的同一 capability；冲突使用稳定 `PluginCapabilityError`，不能静默覆盖。

- [ ] **Step 4: 导出实验入口并运行集成测试**

从 `taifeng.experimental` 导出 `Plugin`, `PluginSpec`, `PluginContext`, `PluginManager`。运行：

`PYTHONPATH=src uv run pytest tests/loop/test_pool_plugins.py tests/plugin -q`

- [ ] **Step 5: 提交**

```bash
git add src/taifeng/loop/pool.py src/taifeng/experimental.py tests/loop/test_pool_plugins.py
git commit -m "feat: load plugins through engine pool"
```

### Task 4: 适配现有 Tool、Hook、Telemetry 和 Skill 扩展点

**Files:**
- Modify: `src/taifeng/tool/registry.py`
- Modify: `src/taifeng/telemetry/sink.py`
- Modify: `src/taifeng/skill/registry.py`
- Modify: `src/taifeng/plugin/context.py`
- Modify: `src/taifeng/loop/event_turn.py`, `src/taifeng/loop/event.py`
- Test: `tests/plugin/test_context.py`
- Test: `tests/telemetry/test_plugin_events.py`

- [ ] **Step 1: 写注册和生命周期事件失败测试**

测试 plugin setup 注册一个 ToolSpec、一个 TelemetrySink 和一个 SkillSource；close 后三者均不可见。测试同名 tool、重复 sink、未知 skill source、active tool registration close 都得到稳定错误。

- [ ] **Step 2: 给每个现有注册动作返回可逆 handle**

在 `PluginContext` 中实现：

```python
tool_handle = ctx.tools.register(spec)
sink_handle = ctx.telemetry.add_sink(sink)
skill_handle = ctx.skills.register_source(source)
```

内部复用现有 `ToolRegistry.register/unregister`、`FilesystemSkillRegistry` 的 snapshot/watcher 语义和 Telemetry sink attach 语义；不要复制一份 registry。直接调用旧 API 的既有返回值和版本行为保持不变。

- [ ] **Step 3: 增加插件生命周期事件**

新增 `PluginLoaded`、`PluginFailed`、`PluginUnloaded`，data 固定为 `plugin_id`, `version`, `scope`, `duration_ms`, `failure_class`，禁止携带 plugin 配置正文、工具参数或外部响应。Console、JSONL、OTel 走现有 generic/专用渲染路径；`EventMsg` seq 仍只由 engine `_emit` 分配。

- [ ] **Step 4: 测试失败回滚和可观测性**

运行：

`PYTHONPATH=src uv run pytest tests/plugin tests/telemetry/test_plugin_events.py tests/loop/test_pool_plugins.py -q`

Expected: plugin setup/close、registration revoke、failure rollback、event mapping 全部通过。

- [ ] **Step 5: 提交**

```bash
git add src/taifeng/tool/registry.py src/taifeng/telemetry/sink.py src/taifeng/skill/registry.py src/taifeng/plugin src/taifeng/loop/event.py src/taifeng/loop/event_turn.py tests/plugin tests/telemetry/test_plugin_events.py
git commit -m "feat: adapt existing registries to plugin handles"
```

### Task 5: 增加 Session/Engine scope 和业务适配器示例

**Files:**
- Create: `src/taifeng/plugin/scope.py`
- Modify: `src/taifeng/plugin/context.py`, `src/taifeng/loop/pool.py`
- Create: `examples/plugin_system/demo.py`
- Test: `tests/plugin/test_scope.py`
- Test: `tests/examples/test_plugin_system.py`

- [ ] **Step 1: 写 scope 隔离失败测试**

创建两个 EnginePool，分别加载同名 plugin；验证 tool/sink/hook 只在所属 pool 可见，关闭 pool A 不影响 pool B。尝试通过 `PluginContext` 取另一个 pool 的 registry 必须抛 `PluginCapabilityError`。

- [ ] **Step 2: 实现 pool scope 和预留 engine/session scope**

第一版完整实现 `scope="pool"`。`PluginSpec.scope` 仍只接受 `pool`；`PluginContext.child(scope="engine")` 只保留内部接口并拒绝公开调用，避免现在就伪造未实现的 per-engine 生命周期。只有第二阶段所有权测试通过后，才把 `engine`/`session` 加入 Literal。

- [ ] **Step 3: 编写业务适配器示例**

示例只使用通用名：一个 `ReadOnlyToolPlugin` 注册无副作用工具，一个 `JsonlAuditSinkPlugin` 订阅事件并写入 `tmp_path` 注入的文件。示例不出现宿主业务字段、tenant、用户数据或外部服务凭据。

- [ ] **Step 4: 运行示例和静态边界检查**

运行：

`PYTHONPATH=src uv run python examples/plugin_system/demo.py`

`PYTHONPATH=src uv run pytest tests/plugin/test_scope.py tests/examples/test_plugin_system.py -q`

新增静态测试扫描 `src/taifeng/plugin/`，拒绝 `tenant_id`、`user_id`、宿主业务名和直接 `os.getenv`。

- [ ] **Step 5: 提交**

```bash
git add src/taifeng/plugin/scope.py src/taifeng/plugin/context.py src/taifeng/loop/pool.py examples/plugin_system tests/plugin tests/examples/test_plugin_system.py
git commit -m "feat: isolate plugin scope and add host adapter example"
```

### Task 6: 增加存储/provider capability seam，不把后端塞进内核

**Files:**
- Modify: `src/taifeng/plugin/context.py`
- Modify: `src/taifeng/conversation/protocols.py`
- Modify: `src/taifeng/conversation/journal/backend.py`
- Modify: `src/taifeng/loop/pool.py`
- Test: `tests/plugin/test_backend_capabilities.py`
- Test: `tests/loop/test_audit_external_journal_core.py`

- [ ] **Step 1: 固定后端 capability 的单一拥有者**

`PluginContext.register_message_store(factory)`、`register_thread_directory(factory)`、`register_journal_core(factory)` 每个 scope 只允许一个 active provider。已有显式 `message_store=`, `thread_directory=`, `audit=AuditConfig(...)` 参数优先；plugin provider 只能在调用方未显式注入时补位。

- [ ] **Step 2: 在首次 await 前冻结 provider 配置**

Pool bootstrap 对 provider factory、writer id、Journal core 和 storage path 做 frozen snapshot；setup 结束后 provider registry 关闭，禁止运行中替换持久化真相源。外部 backend 继续通过现有 `SessionJournalCore` conformance 测试验收。

- [ ] **Step 3: 测试冲突和审计边界**

验证两个 backend plugin 冲突、显式参数覆盖 plugin、audit-required Session 使用 plugin-provided JournalCore、close 时 lease 释放；不能让普通 plugin 绕过 `AuditConfig` 直接拿 lease。

- [ ] **Step 4: 提交**

```bash
git add src/taifeng/plugin/context.py src/taifeng/conversation/protocols.py src/taifeng/conversation/journal/backend.py src/taifeng/loop/pool.py tests/plugin tests/loop/test_audit_external_journal_core.py
git commit -m "feat: expose backend capabilities through plugins"
```

### Task 7: 文档、能力登记和真实回归

**Files:**
- Modify: `docs/architecture/capabilities/plugin-system.md`
- Modify: `docs/architecture/agent-loop.md`
- Modify: `docs/architecture/conversation.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/README.md`
- Modify: `examples/real_llm/capability_matrix.py`
- Modify: `docs/real-llm-ledger.{json,md}` (generated)

- [ ] **Step 1: 同步现状和拒绝边界**

文档必须明确：plugins 是显式注入、第一版 pool scope、无默认 auto-discovery、业务插件在宿主层、active call 不热卸载、PluginManager close 先于 pool 完成释放。登记为实验能力，直到第二阶段 scope/ABI 稳定。

- [ ] **Step 2: 增加真实场景**

在 `capability_matrix.py` 增加 `plugin_tool` 场景：模型调用由 plugin 注册的纯工具，事件流必须出现 `plugin_loaded`、`tool_call_started`、`tool_call_completed`、`plugin_unloaded`；场景不依赖具体业务词。

- [ ] **Step 3: 跑全部门禁**

```bash
PYTHONPATH=src uv run pytest tests/ -q
PYTHONPATH=src uv run python scripts/verify_examples.py
uv run --offline --extra dev ruff check --select F,S108,I,TC src tests examples scripts
uv run --offline --extra dev --extra litellm --extra telemetry-otel mypy src/
uv lock --offline --check
PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py --provider codex --model gpt-5.6-luna
```

Expected: Python 3.12/3.13 full tests pass, examples 40/40, static gates pass, Codex matrix adds plugin_tool PASS and all previous scenarios remain PASS. Generated ledger must be committed with the same source commit.

- [ ] **Step 4: 发布前检查**

确认 `git status --short` 为空、`git rev-list --left-right --count origin/main...main` 符合发布线、`pyproject.toml`/`src/taifeng/__init__.py`/`uv.lock` 版本一致；构建 wheel/sdist 并运行 `scripts/verify_release_artifacts.py`，再决定是否晋升插件 API。

- [ ] **Step 5: 提交文档和能力登记**

```bash
git add docs examples/real_llm/capability_matrix.py docs/real-llm-ledger.json docs/real-llm-ledger.md
git commit -m "docs: register taifeng plugin capability and real regression"
```

## 验收门槛

插件系统完成第一阶段必须同时满足：

1. 无 plugin 参数的现有 EnginePool 行为、工具版本、默认存储和事件流保持兼容。
2. setup 失败不发布半初始化 pool；所有已成功 setup 的 plugin 都按逆序 rollback。
3. 任何 registration 都能通过 handle 撤销；两个 pool 之间没有注册表、sink、hook 或 provider 泄漏。
4. plugin 生命周期事件经过 EventMsg、Console、JSONL 和 OTel；正文、凭据和业务字段不进入事件。
5. `CancellationToken`、close timeout、重复 close、依赖循环、重复 plugin ID、capability 冲突和 active call 卸载边界均有测试。
6. 业务示例不 import 业务系统，核心插件包不包含业务术语。
7. 全量模拟门禁和 Codex 真实回归通过；plugin_tool 场景能证明真实模型确实使用了插件注册能力。

## 分阶段交付顺序

- **P0（必须做）**：Task 1–3，形成可加载、可回滚、可观察的 pool-scoped plugin core。
- **P1（优先做）**：Task 4–5，接入现有 registry 并验证隔离和业务适配器边界。
- **P2（有真实宿主需求再做）**：Task 6，接入 store/Journall/provider factories；不默认启用后端替换。
- **P3（发布前）**：Task 7，能力登记、真实场景、发行产物和文档同步。

