## ADDED Requirements

### Requirement: 运行期增加 skill 目录
`FilesystemSkillRegistry` SHALL 提供 `add_dir(path, *, source)`，成功后产生新快照并返回可撤销的 handle；`source` SHALL 必填。

#### Scenario: 增加目录
- **WHEN** 调用 `add_dir` 传入一个含合法 skill 的目录和 `source="marketplace"`
- **THEN** 这些 skill SHALL 出现在新快照中且 `source` 为 `marketplace`，快照版本 SHALL 递增一次

#### Scenario: 缺少或非法 source
- **WHEN** 不传 `source` 或传入三种取值以外的值
- **THEN** SHALL 被拒绝且不改变任何状态

### Requirement: 重名拒绝
`add_dir` 遇到与当前快照重名的 skill id 时 SHALL 抛 `SkillIdCollisionError`，SHALL NOT 覆盖已有 skill，SHALL NOT 产生新快照。

#### Scenario: 与已有 skill 重名
- **WHEN** 新目录里有一个 id 已存在的 skill
- **THEN** SHALL 报错，快照版本与目录列表 SHALL 保持不变

#### Scenario: 初始化语义不变
- **WHEN** 构造注册表时传入多个目录且存在重名
- **THEN** SHALL 继续按「靠后的目录覆盖靠前的」处理并告警

### Requirement: 可撤销且不残留
handle 关闭后，该目录的 skill SHALL 从下一个快照中消失；关闭 SHALL 幂等；构造时传入的目录 SHALL NOT 可移除。

#### Scenario: 撤销
- **WHEN** 关闭 handle
- **THEN** 该目录的 skill SHALL 不可获取，依赖它们的可达关系 SHALL 同步更新

#### Scenario: 运行中的 turn
- **WHEN** 撤销时有 turn 正在运行
- **THEN** 该 turn SHALL 继续使用它持有的旧快照，移除 SHALL 在下一 turn 生效

#### Scenario: 入口 skill 在用
- **WHEN** 活跃 engine 的入口 skill 位于待移除目录
- **THEN** 经 pool 的移除 SHALL 被拒绝

### Requirement: 活跃会话与 watcher 同步
经 pool 增删目录后，系统 SHALL 让所有活跃 engine 刷新快照，且 SHALL NOT 依赖 `auto_watch_skills`；watcher SHALL NOT 因同一次增删再触发一次重复刷新。

#### Scenario: 未开 watcher
- **WHEN** `auto_watch_skills=False` 且经 pool 增加目录
- **THEN** 活跃 engine SHALL 收到快照刷新

### Requirement: 插件 capability
插件 SHALL 能通过 `ctx.skills.add_dir(path, source=...)` 使用上述能力；pool 的 skill 注册表不支持时 SHALL 抛 `PluginCapabilityError`。

#### Scenario: 自定义注册表
- **WHEN** pool 使用的不是 `FilesystemSkillRegistry`
- **THEN** 调用 SHALL 抛 `PluginCapabilityError` 而不是静默忽略
