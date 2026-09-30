# ADR 0102：审计 Session 关闭时先协作取消在飞的 turn

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §7、§9](../architecture/capabilities/session-journal-business-integration.md)；ADR 0025、0029、0101

## 背景

释放审计 Session 时，Engine 收敛对 operation task 直接 raw cancel。在飞的 root turn 若停在一次
工具调用里，`tool_intent_committed` 已落账、工具还没返回：raw cancel 把它连同「意图收敛」一起
截断——`anyio.fail_after(shield=True)` 挡得住 anyio 的取消，挡不住 `Task.cancel()`。Journal 里留下
一条没有结果的意图，Session 却以 `complete` 终结。

停在 LLM 调用里的 turn 同理：`llm_request_committed` 没有 checkpoint。

派发出去的子 thread 没有这个问题：它们经自己的取消 token 收尾，工具返回 cancelled、意图收敛为终态。
root turn 缺的正是这一步。

## 决策

1. **收敛先协作、后 raw**。`run()` 的收尾先取消持有 root gate 的 turn 的 token，在宽限期（2 秒）内
   等 operation 自行退出；工具经 `ctx.cancel` 给出确定结果，意图收敛为 `cancelled`，在飞的 LLM 调用
   checkpoint 为 `cancelled`。宽限期内没退出的才 raw cancel。
2. **宽限期小于 pool 对 actor 收敛的上限（5 秒）**，否则 pool 会把正常收尾判为无响应。
3. **协作阶段在根取消之前**。根取消一到，排队的消息立即应用；那会抢在在飞 turn 的收尾前面，
   把消息插到一次工具调用和它的结果之间。
4. **收敛期间 gate 不再放行新 turn**。在飞 turn 收尾后空出的 gate 会被排队的消息拿到；此时它们
   只应用、不运行，应用发生在在飞 turn 的记录之后。
5. **没有 turn 在飞或排队时不等**。启动失败等路径上的 operation 不是 turn，照旧立即 raw cancel。

## 影响

- R1–R3：无变化。
- R4：关闭最多多等 2 秒（仅当有 turn 在飞且它不配合取消）。
- R5：释放后的 Journal 没有悬空的意图；接管不会因为上一次正常释放而被拒。

### 行为变化

- 审计模式：释放时在飞的工具调用得到 `cancelled` 结果，在飞的 LLM 调用得到 `cancelled` checkpoint；
  排队的消息在在飞 turn 收尾之后应用。
- 非审计模式无变化。

## 验证

`tests/loop/test_audit_release_inflight.py`（2 项）；`test_audit_queued_submission.py` 里释放场景断言
没有未结算 effect。全量 `pytest tests/` 通过（3792 passed, 17 skipped）；ruff 门禁与 `mypy src/` 清零。
