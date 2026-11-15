# plugin-tool-hook-registration 实施计划

> 前置：`plugin-core`。分支 `feat/plugin-tool-hook`。

## 1. 工具
- [x] 1.1 `tests/plugin/test_context.py` 红测试：注册后 `pool.tool_registry.get(name)` 可见，pool close 后不可见；与内置工具 / `extra_tools` / 其他插件重名抛 `PluginCapabilityError`。Acceptance：观察红。
- [x] 1.2 `plugin/context.py` 加 `ctx.tools.register(spec) -> RegistrationHandle`；`loop/pool_plugins.py` 把 pool 的 `ToolRegistry` 交给 context。Acceptance：测试绿；`tool/registry.py` 无改动。
- [x] 1.3 恢复契约测试（R5）：带插件跑出含插件工具调用的会话，换不带该插件的 pool 恢复，再次调用得到 `unknown_tool` 错误结果而非异常。Acceptance：测试绿。

## 2. 钩子
- [x] 2.1 `tests/hooks/test_registry_unregister.py` 红测试：撤销后 handler 不再执行、撤销不存在的 handler 抛 `ValueError`、执行中撤销不影响本轮。Acceptance：观察红→绿（实现 `HookRegistry.unregister`）。
- [x] 2.2 `ctx.hooks.register(kind, handler) -> RegistrationHandle`；多个插件的钩子按加载顺序执行。Acceptance：`tests/plugin/test_context.py` 相关用例绿。
- [x] 2.3 宿主未传 `hooks` 时按需创建 `HookRunner`；无插件注册钩子时保持 `None`。Acceptance：`tests/loop/test_pool_plugins.py` 两条用例绿，且回填发生在首个 engine 创建之前。

## 3. 验证
- [x] 3.1 更新 `docs/architecture/capabilities/plugin-system.md` 的 capability 清单与重名规则。
- [ ] 3.2 `PYTHONPATH=src uv run pytest tests/ -q`、ruff 门禁、mypy strict。Acceptance：全绿；所有改动文件 ≤ 800 行。
- [ ] 3.3 真实回归 `capability_matrix.py` 并提交台账。Acceptance：原有场景全 PASS。
