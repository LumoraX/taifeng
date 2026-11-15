## ADDED Requirements

### Requirement: pool 之间无泄漏
两个 `EnginePool` 各自加载插件时，一个 pool 中的 tool、hook、skill 目录和 sink 注册 SHALL NOT 在另一个 pool 中可见。

#### Scenario: 同 id 插件分属两个 pool
- **WHEN** 两个 pool 各加载一个同 id 的插件实例
- **THEN** 每个 pool SHALL 只看到自己那份注册

#### Scenario: 关闭其中一个
- **WHEN** 关闭 pool A
- **THEN** pool B 的四类注册 SHALL 全部保持可用

### Requirement: 插件包不含业务概念
`src/taifeng/plugin/` SHALL NOT 出现租户或用户标识字段，SHALL NOT 直接读取环境变量；该约束 SHALL 由自动化测试强制。

#### Scenario: 违规被检出
- **WHEN** 包内出现 `tenant_id`、`user_id`、`os.getenv` 或 `os.environ`
- **THEN** 静态边界测试 SHALL 失败

### Requirement: 可运行且可核验的示例
仓库 SHALL 提供一个不需要 API key 的插件示例，输出带内成功标记，并纳入 examples 冒烟核验。

#### Scenario: 示例运行
- **WHEN** 运行 `examples/plugin_system/demo.py`
- **THEN** 输出 SHALL 包含成功标记，SHALL NOT 包含带内失败标记

### Requirement: 真实模型验证
能力对外声明前，真实回归 SHALL 包含一个由模型调用插件注册工具的场景，并 SHALL 能检出插件未加载的情况。

#### Scenario: 插件工具被调用
- **WHEN** 运行 `plugin_tool` 场景
- **THEN** pool 级 sink SHALL 出现 `plugin_loaded` 与 `plugin_unloaded`，engine 事件流 SHALL 出现该工具的 `tool_call_started` 与 `tool_call_completed`

#### Scenario: 插件缺席
- **WHEN** 场景运行时未加载插件
- **THEN** 场景 SHALL 判为失败

### Requirement: 文档与能力登记
能力对外声明前，能力契约、模块活文档、可配置参数清单、公共 API 文档和能力矩阵 SHALL 已同步，且插件 API SHALL 位于实验层。

#### Scenario: 稳定层不变
- **WHEN** 运行公共 API 快照测试
- **THEN** 稳定层快照 SHALL 无变化，插件相关名称 SHALL 只出现在实验层
