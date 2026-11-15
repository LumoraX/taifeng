## ADDED Requirements

### Requirement: 插件提供存储后端
插件 SHALL 能通过 `ctx.backends` 为 pool 提供 `MessageStore`、`ThreadDirectory` 或 `SessionJournalCore` 的工厂，且提供者 SHALL 在 pool 构建存储之前生效。

#### Scenario: 插件提供 store
- **WHEN** 宿主未传 `message_store=` 且恰有一个插件提供它
- **THEN** pool SHALL 使用该插件提供的 store

### Requirement: 单一提供者且冲突显式
每个后端 capability 每个 pool SHALL 至多有一个提供者；任何冲突 SHALL 抛 `PluginCapabilityError`，SHALL NOT 静默覆盖或静默忽略。

#### Scenario: 两个插件冲突
- **WHEN** 两个插件提供同一后端 capability
- **THEN** `create` SHALL 抛 `PluginCapabilityError`

#### Scenario: 与显式参数冲突
- **WHEN** 宿主显式传了 `message_store=`、`thread_directory=` 或 `audit=`，而插件也提供对应后端
- **THEN** `create` SHALL 抛 `PluginCapabilityError`

### Requirement: 提供者冻结
提供阶段结束后，系统 SHALL 拒绝任何新的后端提供，运行中 SHALL NOT 替换持久化真相源。

#### Scenario: 迟到的提供
- **WHEN** 插件在提供阶段结束后再次提供后端
- **THEN** SHALL 抛 `PluginCapabilityError`

### Requirement: 所有权与清理
pool SHALL 拥有插件提供的后端并在关闭时释放；后续加载步骤失败时 SHALL 关闭已构建的后端。

#### Scenario: 正常关闭
- **WHEN** 调用 `pool.close()`
- **THEN** 插件提供的 store SHALL 被关闭，且晚于插件自身的 close

#### Scenario: 加载中途失败
- **WHEN** 后端已构建而后续某个插件 setup 失败
- **THEN** 已构建的后端 SHALL 被关闭，`create` SHALL 抛出原始错误

### Requirement: 校验与审计边界不被绕过
插件提供的后端 SHALL 经过与显式注入相同的校验；只有 journal 提供者 SHALL 参与审计装配，其他插件 SHALL NOT 获得 lease。

#### Scenario: 不合规的 store
- **WHEN** 插件提供的 store 不满足当前模型客户端或审计模式的要求
- **THEN** SHALL 报出与显式注入时相同的错误

#### Scenario: 审计会话
- **WHEN** 审计会话使用插件提供的 `SessionJournalCore`
- **THEN** 关闭时 lease SHALL 被释放，且普通插件 SHALL NOT 能取得该 lease
