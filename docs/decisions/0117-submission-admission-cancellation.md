# ADR 0117：提交准入即建立取消可达点

状态：Accepted

## 背景

legacy UserMessage 已返回 submission_id，但 actor 派发的任务尚未登记 `_pending`。
紧接提交的 CancelTurn 因而找不到目标，已接受的轮次仍会执行。

## 决策

legacy UserMessage 在入队前登记独立取消 token。执行入口派生 engine 子 token 时同步继承
该登记的取消状态；不依赖宿主轮询等待。排队取消沿用 `turn_failed{kind=cancelled}`。
排队登记用于 introspect 与取消，不作为运行中输入注入的目标。

此修复不扩展审计准入协议，审计路径继续使用 durable coordinator。

## 验证

覆盖提交立即可见、连续提交与取消、root gate 排队与现有审计测试。
