# ADR 0097：审计模式放开挂起与恢复

- 状态：Accepted
- 日期：2026-09-30
- 关联：[session-journal-business-integration §17](../architecture/capabilities/session-journal-business-integration.md)；
  [suspend-resume](../architecture/capabilities/suspend-resume.md)；ADR 0025（Phase 4）、0053、0070、0096

## 背景

ADR 0096 放开了当场作答的权限裁决，把挂起式审批留了下来。需要审计的部署里，审批人很少在线等着：
请求发出去，几分钟到几天之后才有答复，其间进程可能重启、实例可能缩容。审计模式拒绝一切挂起，
等于要求这类部署在「有审计」和「有人工审批」之间二选一。

难点有三处：

1. 挂起的调用已经落了意图、没有结果。恢复扫描会把它当成「结果未知的工具调用」去收敛（ADR 0070）。
2. Session 释放时一律写 `session_ended`，写了之后不能再接管（ADR 0053）。
3. `Resume` 此前在审计模式下直接被拒，没有准入、没有记录。

命中 ADR 0017 规则①。

## 决策

1. **只放开「在工具调用处停下等人作答」的挂起**：挂起式审批（`permission`），以及工具声明
   `can_suspend=True` 后自己发起的填表 / 给数据（`form` / `data`）。这三种的共同点是待答请求指向一次具体的
   工具调用，答复之后那次调用要么重跑、要么直接得到结果，Journal 里意图与结果能一一对上。
2. **等人的调用保持未结算**，不写占位结果。同一批里已有结果的调用照常结算。`turn_suspended` 列出每个
   待答请求对应的意图 record；恢复扫描据此把它们排除在未结算 effect 之外。
3. **结果记在调用原来的 operation 下**，指向挂起前落账的意图。不为重跑另立意图：同一次调用只有一个意图、
   一个结果，这是 §9 的既有不变式。
4. **`Resume` 先过准入、先落账、再入队**，与 `UserMessage` 同一套 admission 机制。`resume_accepted`
   保存答复原文——答复是人的决定，是审计最想留下的东西。
5. **一次 `Resume` 必须答复全部请求**。分批答复意味着挂起处于「部分结清」的中间态，接管、重放、
   重复提交时都要处理它；审计模式下不引入这个状态。
6. **`resume_applied` 写在续跑的 turn 之前**，含义是「答复已经落到挂起上」。最初的实现把它写在续跑结束之后，
   结果是终态事件发出之后 Journal 还在写：调用方看到 `turn_completed` 随即释放 Session，写入被取消，
   写者进入结果未知状态，Session 冻结。续跑的 turn 怎么结束由它自己的记录说明，与普通 turn 一致。
7. **等待期间释放 Session 写 `session_detached`**，不写 `thread_terminal` 与 `session_ended`。
   是否在等待由 Session 级的登记判断（挂起落账后登记、结清落账后注销、接管时由 Journal 重建），
   不从 engine 的内存 history 推断：没有进 Journal 的挂起在接管之后不存在，不能据它决定不终结。
8. **处置到一半中断后重新提交即可**。`resume_accepted` 已落账而 `suspension_resolved` 没有时，
   挂起仍然有效；重新提交时已经结算过的调用沿用已有结果。不为此引入专门的恢复记录。
9. **未声明 `can_suspend` 的工具自行挂起仍是能力违约**。声明是静态的，审计配置的评审者看得到；
   运行时才发现一个工具会停下等人，说明配置评审依据的信息不完整。
10. **续跑的 turn 登记为可取消的目标**。`CancelTurn` 指向这次 `Resume` 的 submission id 即可取消它。

## 不做

- **子 skill 内的挂起**：需要沿派发树保存每一层的断点，且父调用的 `skill_dispatch_started` 此时没有对应的
  finished。留给后续。
- **带到期时间的挂起**：到期裁决是内核自己发起的处置，要有自己的记录与定时器的接管语义。
- **失败处置挂起、资源护栏挂起**：待答请求不指向一次工具调用，处置方式是重试一次采样或中止，模型不同。
- **`CancelTurn` 核销挂起**：非审计模式下它往 store 直接追加结清标记。审计模式下要放弃一次挂起，
  用 `Resume` 拒绝其中的请求，处置与记录都走同一条路径。
- **分批答复**：见决策 5。

## 影响

- R1：无业务概念。答复内容对内核不透明。
- R2：挂起与恢复不触发压缩；结清标记不进入模型可见的上下文（既有行为）。
- R3：既有事件不变（`turn_suspended` / `suspension_resolved` / `suspension_resolve_rejected`）。
  审计模式下被拒的 `Resume` 在入队之前抛异常，不发 `suspension_resolve_rejected` 事件；准入之后挂起被别的途径
  结清的情形仍发该事件。
- R4：续跑的 turn 可取消；结算与结清的落账与取消无关。
- R5：等待期间可以释放、崩溃、接管；接管后的 history 与释放前一致。

### 行为变化

- 审计模式的静态门不再拒绝 `SuspendingPrompter`、`can_suspend=True` 的工具与名为 `request_user_input`
  的工具；`audit_tool_suspension_unsupported` 不再出现。
- 内置的 `request_user_input` 声明 `can_suspend=True`。
- 审计模式的 `submit()` 接受 `Resume`。
- 非审计模式无变化。

### 已知边界

- 结清之后、获批的调用重跑结束之前崩溃：该调用按 ADR 0070 收敛（纯 / 幂等的重试，其余回查或交人）。
- 答复原文进 Journal；敏感内容由业务在提交前处理。

## 验证

`tests/loop/test_audit_suspension.py`（14 项）：静态门、挂起落账与未结算、混合批次、能力违约冻结、
批准 / 拒绝 / 作答 / 两个请求一次答复、不适用的 `Resume` 被拒且状态不变、释放后接管、崩溃后接管、
处置中途中断后重新提交、`CancelTurn` 不核销挂起。按新契约更新了 8 项旧测试（原先断言「挂起一律被拒」）。
全量 `pytest tests/` 通过；ruff 门禁与 `mypy src/` 清零。真实 LLM 台账随集成一并刷新。
