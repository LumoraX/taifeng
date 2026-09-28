# ADR 0049：取消 token 携带原因与墙钟截止时间，采样流与取消竞速

- 状态：Accepted
- 日期：2026-09-28
- 关联：[agent-loop § Cancellation](../architecture/agent-loop.md)；[turn-resource-guards](../architecture/capabilities/turn-resource-guards.md)；K5（取消终态守卫）

## 背景

2026-09-28 内核 review：`CancellationToken` 只有「是否取消」一个比特。① 终态分不清用户中止、超时、
关停；② 没有墙钟截止时间——宿主拿不到 spawn 深层子树的 token，无法给整棵子树设上限，只能靠迭代
预算间接约束；③ 核实时进一步发现：采样循环只在**收到流事件时**检查取消，provider 在首字节前长时间
阻塞（长思考、网关卡住）时，取消 / 截止时间要等到下一个事件才生效，实测 sim 下被拖满整个延迟。

## 决策

1. **原因随取消级联**（Go `context.Cause` 范式）：`cancel(reason, detail)`，后代看到祖先的原因；
   首次取消的原因生效。枚举而非自由字符串（魔法值红线）。
2. **截止时间挂在子树根**：`child(deadline_seconds=)` / `set_deadline`，到点以 `DEADLINE_EXCEEDED`
   取消。父的截止时间经级联天然覆盖子树，不需要把 deadline 复制给每个后代；只收紧不放宽。
3. **入口**：`UserMessage.deadline_seconds`（每条消息）与 `spawn_skill(deadline_seconds=)`。不加 pool 级
   默认旋钮——上限是策略（R1），宿主按消息设置即可；也避免给已超行数红线的 `pool.py` 继续加参数。
4. **取消原地打断阻塞读取**：`interrupt_on_cancel` 在 token 取消时对当前 task 调 `cancel()`，把阻塞中的
   流读取就地打断；退出时 `uncancel` 并改抛带原因的 `CancelledError`（`asyncio.timeout` 同一手法），外层仍
   按「token 取消 → 优雅终结」处理（K5）。**否决「每取一项在独立 task 里与取消竞速」**：审计路径的
   observed session 要求流只能在 owner task 迭代，另起 task 读取会被拒（实测导致审计取消用例挂起）。
5. **终态透出** `turn_completed.cancel_reason`。

## 未做

工具 handler 仍是协作式取消（K5 结论不变）：不响应 token 的阻塞工具由 `ToolSpec.timeout_seconds` 兜底。
