# 短命消息存储能力

## 目的

`EphemeralMessageStore` 是对 Responses 短命执行的显式公共适配。它实现现有
`MessageStore` 与 `AtomicBatchMessageStore`，只把当前实例内的消息和终态批次保存在内存中。

## 契约

- `append_atomic_batch` 对同一 thread 的非空批次提供全有或全无可见性。
- 相同 `batch_id` 与相同规范摘要重试返回 `BatchAppendAck(already_committed=True)`；不同内容抛出
  `BatchConflictError`。
- 消息在读取时返回快照，调用方修改返回对象不会改变存储中的内容。
- `close()` 清空 thread、消息和批次；关闭后所有读取与写入抛出
  `EphemeralStoreClosedError`。
- 进程崩溃、重启或实例关闭后不提供恢复、审计或持久化承诺。

## 边界

该能力不改变默认 `JsonlMessageStore` 的持久化语义，也不放宽 `EnginePool` 对 Responses
原子存储的检查。审计模式仍使用默认持久化路径；业务方只能显式选择短命实例，并负责在
执行结束时关闭它。短命实例不承载业务、租户或外部连接概念。
