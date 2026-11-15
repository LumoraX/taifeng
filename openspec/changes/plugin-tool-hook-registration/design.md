## Context

来源：计划 r2 的 Task 4。前置：`plugin-core` 已合入。分支 `feat/plugin-tool-hook`。

## Goals / Non-Goals

目标是让插件通过 context 注册工具和钩子，pool 关闭后不残留。不复制注册表，不改变直接调用旧 API 的行为。

## Decisions

1. **工具走现有共享注册表**。`ctx.tools.register(spec)` 内部直接调 `ToolRegistry.register`，handle `aclose()` 调 `unregister`。pool 的工具注册表本来就是全 pool 共享，`bind_tool_set_events` 负责广播，无需新机制。
2. **与 MCP bridge 同一套约定**。`mcp/bridge.py` 已在运行期对同一个注册表 register / replace / unregister，插件沿用它的做法。
3. **重名一律报错**。与内置工具、`extra_tools`、其他插件重名时，把 `DuplicateToolError` 包成 `PluginCapabilityError`。插件先占名、MCP 后注册同名工具时，MCP 侧拿到既有的 `DuplicateToolError`。
4. **钩子的目标**是 `hooks/types.py` 的 `HookRunner`（`EnginePool.create(hooks=...)` 那个）。仓库里另有 `conversation/hook_runner.py` 的同名类，管索引钩子，不在范围内。
5. **`HookRegistry.unregister(kind, handler)`** 按对象身份移除，不存在抛 `ValueError`。`handlers()` 已返回副本，执行中撤销不影响本轮迭代。
6. **按需创建 runner**。宿主没传 `hooks` 且有插件注册钩子时，`pool_plugins` 新建 `HookRunner` 交给 pool；没有插件注册钩子时保持 `hooks=None`。因为加载点在 `cls(...)` 之后，需要 pool 提供一个在首个 engine 创建前设置 `_hooks` 的受控入口。

## Risks

- 决策 6 需要在 pool 构造后回填 `_hooks`；必须保证发生在任何 engine 创建之前，并有测试固定。
- 不跟踪在途调用的前提是「handle 只在 pool close 时关闭」。若将来支持运行中卸载，需要先给 `ToolCallRuntime` 加在途计数。
