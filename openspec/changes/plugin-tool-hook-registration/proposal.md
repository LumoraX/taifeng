## Why

`plugin-core` 只给了生命周期，插件还不能注册任何东西。工具和钩子是宿主最常扩展的两处，且都已有共享注册表可复用。

## What Changes

- `PluginContext` 增加 `tools` 与 `hooks` 两个 capability，注册返回可撤销的 `RegistrationHandle`。
- `HookRegistry` 新增 `unregister(kind, handler)`（今天只有 `register`）。
- 宿主未传 `hooks` 而插件要注册钩子时，由 pool 接线新建一个 `HookRunner`。

## Capabilities

### New Capabilities

- `plugin-tool-hook-registration`：插件注册 / 撤销工具与钩子。

### Modified Capabilities

无（`HookRegistry.unregister` 是纯新增）。

## Non-goals

不改 `ToolRegistry` 的既有 API 与 `version` 语义；插件不能 `replace` 工具；不碰 `conversation/hook_runner.py` 的索引钩子 runner；不跟踪在途工具调用。

## R1–R5 影响

- R1：无业务字段。
- R2：工具集进 prompt 前缀。插件在首个 engine 前注册、pool close 时撤销，两个时点都没有活跃 Session，不产生缓存失效。
- R3：复用既有 `tool_set_changed`；不新增事件。
- R4：无新增长时操作。
- R5：插件缺席时恢复含插件工具调用的会话，沿用现有行为——`ToolCallRuntime` 返回 `unknown_tool` 错误结果（`reason="not_in_registry"`）。

## Impact

实现：`src/taifeng/plugin/context.py`、`src/taifeng/hooks/types.py`、`src/taifeng/loop/pool_plugins.py`。

测试：`tests/plugin/test_context.py`、`tests/hooks/test_registry_unregister.py`、`tests/loop/test_pool_plugins.py`。

文档：更新 `docs/architecture/capabilities/plugin-system.md`。改动 `loop/`，合入前须跑真实回归。
