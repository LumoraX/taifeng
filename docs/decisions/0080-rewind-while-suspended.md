# ADR 0080：挂起态下允许 rewind，挂起随截断一并作废

- 状态：Accepted（Amends #0014、#0018）
- 日期：2026-09-30
- 关联：[turn-rewind 契约 § 挂起态下的 rewind](../architecture/capabilities/turn-rewind.md)；
  [suspend-resume 契约](../architecture/capabilities/suspend-resume.md)；ADR 0012 / 0014 / 0018 / 0079

## 背景

ADR 0014 把挂起态 rewind 列为 v1 不支持：存在活跃挂起 record 时一律 `rewind_rejected(turn_suspended)`，理由是
「与 Resume 职责重叠」。但两者回答的是不同问题：Resume 是「回答这个问题」，Rewind 是「不回答，回到之前重来」。
挂起恰恰是人最想改主意的时刻——审批请求弹出来，人发现模型走偏了，想让它回到上一步换个做法。此前唯一的办法是先
`CancelTurn`（整个 turn 作废）再重新提问，前面已完成的步骤全部重做。

## 决策

1. **挂起态不再拦 rewind**。截断把挂起 record 连同它等待的调用一起带出逻辑 history，挂起就此作废。
2. **不另写 resolved-marker**。`CancelTurn` 作废挂起要写 marker，是因为 record 还留在逻辑 history 里；rewind 的
   截断已经把 record 移出逻辑 history，热内存与冷重建都看不到它，再写 marker 是冗余状态。作废的事实由
   `turn_rewound.discarded_suspension` 透出。
3. **守卫判定「截断后的 history 是否自洽」，不判定「是否处于挂起态」**。纯函数
   `suspended_rewind_rejection`：
   - 挂起 record 仍在保留范围内 → `turn_suspended`（节点在挂起之后，正常不会出现，防御）；
   - 还留着没有结果的调用、且不是本次要重跑的那一个 → `sibling_calls_pending`。
4. **`retry_tool` 只能作用于正在等人的那个调用**。对同批已有结果的调用做 retry_tool，会让同批等人的调用随挂起作废
   而永远悬空——既没有结果，也没有人会再回答。不替它们合成结果、也不连带重跑：前者伪造事实，后者重复副作用。
   `re_reason` 不留任何调用，不受此限。
5. **被拒的 rewind 不改动任何状态**：规划与守卫在改动 history 之前完成，挂起保持活跃。
6. **根路径与 spawn 子 thread 路径共用规划函数 `plan_rewind`**；spawn 路径的统一驱动入口放行 `suspended` 状态。

## 替代方案

- **先隐式 `CancelTurn` 再 rewind**：多写一条 marker、多发一组取消事件，且两步之间崩溃会停在「turn 已取消、
  未 rewind」；一步完成更简单；否决。
- **挂起态 retry_tool 连带重跑同批全部等人的调用**：它们会各自重新挂起，语义上等于「把整批问题重新问一遍」，
  而 `re_reason` 已经覆盖「整批重来」；否决。
- **为同批等人的调用合成「已取消」结果**：让模型看到人从未给出的答复；否决。
- **递归作废嵌套子 thread 上的挂起 record**（写 resolved-marker 到各子 thread）：子 thread 的 Resume 经根的活跃
  挂起寻址，根已无活跃挂起即被拒，子 record 不可达；递归写 marker 只增加写入与失败窗口；否决。

## 后果

- `turn_rewound.data` 新增 `discarded_suspension`；`rewind_rejected.reason` 新增 `sibling_calls_pending`。
- 依赖「挂起时 rewind 必被拒」的业务逻辑需调整：挂起态下的 rewind 现在会生效。
- 挂起 record 的 TTL 到期处理在 record 已不在逻辑 history 时为 no-op（与 `CancelTurn` 作废后的行为一致）。
- R1–R5：R1 无业务概念；R2 同普通 rewind；R3 见上；R4 重推沿用既有取消语义；R5 store append-only，
  冷重建后无活跃挂起、与热内存一致。

## 验证

`tests/loop/test_rewind_suspended.py`：re_reason 作废挂起且旧请求不可再 Resume；对挂起调用 retry_tool 换参后重新
挂起、Resume 后以新参数执行一次；同批有调用等人时对已结算调用 retry_tool 被拒且状态不动、挂起仍可 Resume；
同批有调用等人时 re_reason 放行；挂起时回到更早的 turn；冷加载后无活跃挂起且与热内存逐项相同；
未挂起时 `discarded_suspension` 为 null；挂起的 spawn 经 rewind 重推至完成。
`tests/loop/test_rewind_thread.py`：挂起的 spawn 上被拒的 rewind 不作废挂起。
