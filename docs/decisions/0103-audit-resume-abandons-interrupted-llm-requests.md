# ADR 0103：接管时作废没有 checkpoint 的 LLM 请求

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §13.5](../architecture/capabilities/session-journal-business-integration.md)；
  ADR 0025、0053、0070、0076、0098

## 背景

审计 Session 接管时，任何没有 checkpoint 的 `llm_request_committed` 都使 resume 被拒。一个 turn 的
大部分时间都在等模型：进程死在 LLM 调用途中是最常见的崩溃时刻，而恰恰是这种 Session 无法接管。
工具调用（ADR 0070）、从未登记意图的调用（ADR 0075）、被中断的派发（ADR 0076、0098）都有了收敛规则，
LLM 请求是最后一块。

ADR 0025 把未匹配的 intent 一律视为 UNKNOWN，理由是 effect 可能已经发生。这条理由对工具成立，
对 LLM 请求不成立：模型的回复只有进了 Journal（checkpoint）才会进对话；没有 checkpoint，回复一个字
也没有被谁看到。作废它不会重复任何事情——下一个 turn 重新采样，模型看到的上下文和当时一样。

命中 ADR 0017 规则①。

## 决策

1. **可收敛 thread 上没有 checkpoint 的 LLM 请求作废**：落 `llm_request_abandoned`，此后视为已结算。
   可收敛 thread 的范围与工具调用相同（root、被中断派发的子 thread、被中断的分离式派发与聚合 turn 的 thread）。
2. **那个 turn 到此为止，不补跑**。对话停在请求发出之前的位置；模型在下一个 turn 继续。补跑等于替
   业务决定「再问一次」，而这个决定该由发起下一个 turn 的人做。
3. **有 checkpoint 就不作废**。checkpoint 是已经到达的回复，哪怕后面没有 `llm_response_committed`，
   按既有规则它已经结算。
4. **作废记录与同 thread 上工具调用的结论同批**，先于派发 / 派发出去的 thread 的终态：一个被中断的
   子 thread 先把自己的事收干净，再由派发的终态盖章。

## 影响

- R1–R4：无变化。
- R5：进程死在 LLM 调用途中的 Session 可以接管；接管后的对话与崩溃前模型看到的一致。

### 行为变化

- 此前被拒（`audit_resume_recovery_required`）的这类 Session 现在可以接管。
- 不可收敛 thread（不属于任何被中断派发的子 thread）上的请求仍 fail closed，与其他未结算项相同。

## 验证

`tests/loop/test_audit_resume_llm.py`（3 项）：root turn、分离式派发的子 thread、同步派发的子 thread
各在 LLM 调用途中崩溃，接管后请求作废、派发落终态、下一个 turn 正常。全量 `pytest tests/` 通过
（3795 passed, 17 skipped）；ruff 门禁与 `mypy src/` 清零。
