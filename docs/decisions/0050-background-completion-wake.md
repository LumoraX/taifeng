# ADR 0050：后台任务完成时唤醒发起方，而非只能轮询

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/tool-builtins-extended.md](../architecture/capabilities/tool-builtins-extended.md)；[peer-mailbox-messaging](../architecture/capabilities/peer-mailbox-messaging.md)

## 背景

`run_in_background` 起的任务只能靠 `wait_for_task` 轮询取结果：模型要么阻塞等待（失去后台的意义），
要么忘了去取。参照 openclaw `bash-tools.exec-runtime.ts` 的 `notifyOnExit`。

## 决策

1. **注册表提供完成回调**（`spawn(on_complete=)`），机制与投递解耦：注册表不知道 engine。
2. **投递复用 peer mailbox**，沿用它的唤醒规则而不另立：根 thread 只 `queue_only`（根 turn 由宿主驱动，
   宿主可据 `background_task_completed` 事件决定是否提交新 turn）；spawn 子 thread `trigger_turn`；
   `call_skill` 阻塞子 thread 本是根 turn 的一部分、不可寻址 → 明确改投根 thread（事件带 `delivered_to`）。
3. **默认开**（`notify_on_exit=True`）：通知只是一条中性事实，模型仍可 `wait_for_task` 取全文。
4. 摘要只带 stdout 尾部（≤2000 字符），防大输出挤占上下文。
