# plugin-core 实施计划

> 前置：无。分支 `feat/plugin-core`。先契约、红测试、实现；每步完成即 commit。

## 1. 契约与类型
- [ ] 1.1 写 `docs/architecture/capabilities/plugin-system.md`（数据结构、状态机、事件字段、冲突规则、R1–R5、不做的事）与 `docs/decisions/0144-plugin-lifecycle-and-scope.md`。Acceptance：ADR 编号不与 `0143-stable-execution-identity.md` 冲突；`docs/README.md` 索引已加。
- [ ] 1.2 `tests/plugin/test_context.py` 先写红测试：`PluginSpec` 冻结；context 公开成员恰为 `{"plugin_id", "cancel"}`；封口后注册被拒。Acceptance：观察红。
- [ ] 1.3 新增 `plugin/errors.py`（`DuplicatePluginError`、`MissingPluginDependencyError`、`PluginDependencyCycleError`、`PluginSetupError`、`PluginCloseError`、`PluginCapabilityError`）、`plugin/types.py`、`plugin/context.py`、`plugin/__init__.py`。Acceptance：`PYTHONPATH=src uv run pytest tests/plugin/test_context.py -q` 全绿；错误消息只含 plugin id、capability 名和稳定 code。

## 2. 事件与 manager
- [ ] 2.1 `tests/telemetry/test_plugin_events.py` 红测试，再在 `loop/event_lifecycle.py`、`event_base.py`、`event.py` 加三个事件；`telemetry/console.py`、`otel_sink.py` 加渲染 / 映射。Acceptance：Console / JSONL / OTel（`importorskip`）三路通过；事件不含配置正文。
- [ ] 2.2 `tests/plugin/test_manager.py` 红测试：依赖顺序与逆序关闭、setup 失败回滚（`["setup:a", "setup:bad", "close:a"]`）。Acceptance：观察红。
- [ ] 2.3 新增 `plugin/manager.py`：Kahn 排序、回滚、`close(timeout_seconds=5.0)`、`snapshot()`。Acceptance：上述测试绿。
- [ ] 2.4 补边界测试：setup 中取消、close 超时进入 `closed_with_errors`、重复 close、重复 id、缺依赖、循环依赖、`rollback_errors` 计数。Acceptance：`PYTHONPATH=src uv run pytest tests/plugin tests/telemetry/test_plugin_events.py -q` 全绿。

## 3. EnginePool 接线
- [ ] 3.1 `tests/loop/test_pool_plugins.py` 红测试：插件在首个 engine 前完成 setup、`pool.close()` 后插件已关闭。测试内自建 sim client helper，复用 `tests/conftest.py` 的 `skills_dir`。Acceptance：观察红。
- [ ] 3.2 新增 `loop/pool_plugins.py`；`pool.py` 加 `plugins: Sequence[Plugin] = ()`；`pool_lifecycle._cleanup_pool_resources` 最前面关闭 manager。Acceptance：测试绿；`wc -l src/taifeng/loop/pool.py` ≤ 800。
- [ ] 3.3 默认路径测试：未传 `plugins` 时不创建 manager、工具注册表 `version` 不变、不发任何插件事件。加载失败测试：`create` 抛 `PluginSetupError` 且 store / directory 已关闭。Acceptance：全绿。
- [ ] 3.4 `experimental.py` 导出 `Plugin`、`PluginSpec`、`PluginContext`、`PluginManager`、`RegistrationHandle`。Acceptance：`PYTHONPATH=src uv run pytest tests/test_public_api.py -q` 全绿，稳定层快照不变。

## 4. 验证
- [ ] 4.1 `PYTHONPATH=src uv run pytest tests/ -q`、`uv run ruff check --select F,S108,I,TC src tests examples scripts`、`uv run --extra dev --extra litellm --extra telemetry-otel mypy src/`。Acceptance：全绿。
- [ ] 4.2 真实回归 `PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py` 并提交台账（改了基础层 `loop/`）。Acceptance：原有场景全 PASS。
