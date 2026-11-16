## ADDED Requirements

### Requirement: 多 sink 分发
`FanoutSink` SHALL 实现 `TelemetrySink` 协议，按加入顺序把每个事件投递给全部成员，并支持通过 handle 移除成员。

#### Scenario: 顺序投递
- **WHEN** 先后加入两个 sink 并处理一个事件
- **THEN** 两个 sink SHALL 各收到一次，且先加入的先收到

#### Scenario: 移除成员
- **WHEN** 关闭某成员的 handle
- **THEN** 该成员 SHALL 不再收到后续事件

#### Scenario: 重复加入
- **WHEN** 同一 sink 对象被加入两次
- **THEN** 第二次 SHALL 抛 `ValueError`

### Requirement: 成员故障隔离
单个成员处理事件失败时，系统 SHALL 记录日志并继续投递给其余成员，`handle` SHALL NOT 向调用方抛出该错误；取消 SHALL 照常传播。

#### Scenario: 成员抛错
- **WHEN** 第一个成员抛出普通异常
- **THEN** 第二个成员 SHALL 仍收到事件，调用方 SHALL NOT 看到异常

#### Scenario: 取消
- **WHEN** 投递过程中发生取消
- **THEN** 取消 SHALL 向上传播而不被吞掉

### Requirement: 未使用插件时不包装
未传 `plugins` 时，pool SHALL 把宿主的 `sink` 原样交给下游，对象身份 SHALL 不变。

#### Scenario: 默认路径
- **WHEN** 调用方只传 `sink=` 不传 `plugins`
- **THEN** 下游使用的 sink SHALL 与传入的是同一对象

### Requirement: 插件 sink 的事件范围
插件通过 `ctx.telemetry.add_sink(sink)` 注册的 sink SHALL 收到其加入之后的 pool 级事件，包括插件生命周期事件；系统 SHALL NOT 在第一版向它转发 engine 的 turn / tool 事件流。

#### Scenario: 宿主 sink 优先
- **WHEN** 宿主传了 `sink=` 且插件也注册了 sink
- **THEN** 宿主 sink SHALL 排在最前并收到全部 pool 级事件

#### Scenario: 加入之前的事件
- **WHEN** 某事件在插件 sink 加入之前发出
- **THEN** 该 sink SHALL NOT 收到它，系统 SHALL NOT 补发
