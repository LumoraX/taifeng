## ADDED Requirements

### Requirement: 插件注册可撤销的工具
插件 SHALL 能通过 `ctx.tools.register(spec)` 把工具加入 pool 共享的工具注册表，并得到可撤销的 handle；系统 SHALL NOT 改变直接调用 `ToolRegistry` 的既有行为。

#### Scenario: 注册与撤销
- **WHEN** 插件在 setup 中注册工具，随后 pool 关闭
- **THEN** 工具在 pool 存活期间 SHALL 对所有 engine 可见，pool 关闭后 SHALL 不再存在于注册表

#### Scenario: 重名
- **WHEN** 插件注册的工具名与内置工具、`extra_tools` 或其他插件的工具相同
- **THEN** SHALL 抛 `PluginCapabilityError`，SHALL NOT 覆盖已有工具

### Requirement: 插件缺席时的恢复行为
恢复一段含插件工具调用的会话而该插件未加载时，系统 SHALL 返回 `unknown_tool` 错误结果，SHALL NOT 抛出未分类异常。

#### Scenario: 换 pool 恢复
- **WHEN** 会话在不带该插件的 pool 中恢复并再次调用该工具
- **THEN** 工具结果 SHALL 为错误且 `reason="not_in_registry"`

### Requirement: 插件注册可撤销的钩子
插件 SHALL 能通过 `ctx.hooks.register(kind, handler)` 注册钩子；`HookRegistry` SHALL 提供按对象身份移除的 `unregister`。

#### Scenario: 执行顺序
- **WHEN** 多个插件对同一 kind 注册钩子
- **THEN** SHALL 按插件加载顺序串行执行，任一拒绝即整体拒绝

#### Scenario: 撤销
- **WHEN** handle 被关闭
- **THEN** 该 handler SHALL 不再被执行；撤销不存在的 handler SHALL 抛 `ValueError`

#### Scenario: 宿主未配置钩子
- **WHEN** 宿主未传 `hooks` 且没有任何插件注册钩子
- **THEN** pool 的 `hooks` SHALL 保持 `None`
