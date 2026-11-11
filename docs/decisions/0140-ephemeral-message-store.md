# ADR 0140：显式短命消息存储

## 状态

已接受

## 背景

Responses 的一类受限执行需要把原文和终态事件留在当前进程内存，不能把它伪装成
`AtomicBatchMessageStore` 所要求的持久确认。默认 JSONL、数据库和审计路径必须继续保持
崩溃可恢复语义。

## 决策

新增 `EphemeralMessageStore`，独立实现 `MessageStore` 与 `AtomicBatchMessageStore`。它只承诺
当前实例生命周期内的原子批次、幂等重试和冲突拒绝；`close()` 清空全部状态，崩溃和重启均
视为数据丢失。该类不加入租户、业务或 provider 概念，也不改变现有 pool 的能力检查。

## 后果

宿主可以显式选择不可恢复的短命 Responses 执行，而持久化用户仍沿用原有 store。调用方必须
把短命执行限制在允许的无外部副作用范围，并在结束时关闭实例。需要恢复、审计或跨进程共享
时，必须使用持久 `MessageStore`，不能把本类作为替代。
