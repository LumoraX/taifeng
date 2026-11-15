## ADDED Requirements

### Requirement: 显式注入且默认路径不变
系统 SHALL 只通过 `EnginePool.create(plugins=...)` 接受插件；未传时 SHALL 不创建 manager、不改变工具注册表版本、不发出任何插件事件。

#### Scenario: 未传插件
- **WHEN** 调用方不传 `plugins`
- **THEN** pool 的行为、默认存储、sink 对象身份和事件流 SHALL 与引入插件前一致

#### Scenario: 首个 engine 之前完成加载
- **WHEN** 调用方传入插件并 `create` 返回
- **THEN** 所有插件的 setup SHALL 已完成，且此时尚未创建任何 engine

### Requirement: 依赖顺序与加载前校验
manager SHALL 按 `requires` 拓扑顺序加载，同层按传入顺序；重复 id、缺依赖、循环依赖 SHALL 在任何 setup 运行前被拒绝。

#### Scenario: 依赖顺序
- **WHEN** 传入 `(B, A)` 且 B 依赖 A
- **THEN** SHALL 先 setup A 再 setup B，close 时先 B 后 A

#### Scenario: 非法依赖图
- **WHEN** 存在重复 id、缺失依赖或环
- **THEN** SHALL 抛出对应的稳定错误，且没有任何插件的 setup 被调用

### Requirement: setup 失败回滚
任一插件 setup 失败时，系统 SHALL 撤销该插件已做的注册并逆序关闭此前已成功的插件，SHALL NOT 产出半初始化的 pool。

#### Scenario: 中途失败
- **WHEN** 第二个插件的 setup 抛错
- **THEN** `create` SHALL 抛 `PluginSetupError`，第一个插件 SHALL 已被关闭，store 与 directory SHALL 已被清理

#### Scenario: 回滚自身出错
- **WHEN** 回滚过程中某个 close 抛错
- **THEN** 原始 setup 错误 SHALL 保留为首因，回滚错误 SHALL 只计入 `plugin_failed.rollback_errors`

### Requirement: 有界且幂等的关闭
`pool.close()` SHALL 在全部 Session 释放之后、store 关闭之前逆序关闭插件；每个插件 SHALL 受超时约束，单个失败 SHALL NOT 阻断其余清理。

#### Scenario: close 超时
- **WHEN** 某插件 close 超过超时上限
- **THEN** manager SHALL 进入 `closed_with_errors`，其余插件与 pool 资源 SHALL 继续清理

#### Scenario: 重复关闭
- **WHEN** 再次调用 close
- **THEN** SHALL 无副作用地返回

### Requirement: 取消传播
插件 SHALL 通过 `ctx.cancel` 拿到由 pool 根令牌派生的 `CancellationToken`；setup 期间被取消 SHALL 触发回滚。

#### Scenario: setup 中取消
- **WHEN** setup 等待期间令牌被取消
- **THEN** SHALL 回滚已加载插件并向上传播取消

### Requirement: capability allowlist 与封口
`PluginContext` SHALL 只暴露显式列出的成员，SHALL NOT 暴露 engine、runner、prompt 或 history；setup 返回后 SHALL 拒绝任何新的注册。

#### Scenario: setup 之后注册
- **WHEN** 插件在 setup 返回后调用注册方法
- **THEN** SHALL 抛 `PluginCapabilityError`

### Requirement: 生命周期事件
系统 SHALL 发出 `plugin_loaded`、`plugin_failed`、`plugin_unloaded`，字段固定为 `plugin_id`、`version`、`scope`、`duration_ms`、`failure_class`、`rollback_errors`，SHALL NOT 携带插件配置、工具参数或外部响应。

#### Scenario: 无 engine 时发事件
- **WHEN** 插件在 pool 创建期间加载
- **THEN** 事件 SHALL 以 `submission_id="*"` 交给 pool 级 sink；未配置 sink 时 SHALL 只记日志而不报错
