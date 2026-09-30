# ADR 0105：replay 模式——工具结果回放与整条 Session 重放

- 状态：Accepted
- 日期：2026-09-30
- 关联：[journal-replay](../architecture/capabilities/journal-replay.md)；ADR 0014（留作 replay 模式）、0054、0098

## 背景

ADR 0014 把「录后确定性重放整条 call 图」留作未来的 replay 模式。ADR 0054 做了一半：LLM 的回复可以
从 Journal 回放。另一半是工具——工具结果仍要真的执行，重放一段访问过外部系统的对话就会再访问一次；
还缺把录制的提交序列（用户消息、答复）原样送进新 Engine 的驱动。

命中 ADR 0017 规则①。

## 决策

1. **工具结果按（工具名，有效参数）从录制匹配**，同名同参的多次录制按录制顺序消费，与 LLM 回放同一
   套「按内容而非顺序、分叉即报错」的规则。数据源是 Journal 里已有的意图与结果，不另建录制格式。
2. **内核编排工具照常运行**。`call_skill` / `spawn_skill` / `await_skills` / `send_message` 这些工具的
   效果是内核自己的行为——子 turn、句柄、消息——重放时由内核重现，它们内部的 LLM 调用照样从录制匹配。
   把它们也换成录制结果，子 turn 就不会跑，call 图只剩表皮。
3. **派发句柄由（turn 序号，调用 id）派生**。句柄 id 进入工具结果、进入模型看到的上下文；随机的句柄
   让每次重放的上下文都不一样，LLM 匹配必然分叉。审计模式下子 thread id 已由句柄派生，于是用同一个
   Session id、独立的 Journal 根重放，得到同样的 thread id。
4. **整条重放按录制的提交序列驱动**，每一步等 root turn 停下再送下一步；分叉即停并报告在哪一步。
   录制里停下等人的调用若在重放里直接拿到了录制的结果（工具被替换），对应的 `Resume` 记为
   `not_needed`：那次审批已经在录制里发生过。
5. **重放不比较对话内容**。内核的行为由 LLM 与工具的回复决定，两者都来自录制；新一轮运行走了录制里
   没有的路时匹配失败就是分叉。要对照细节，重放本身在审计模式下留下一份新 Journal。
6. **`JournalReplayClient` 列入 attempt-observable 的已审查客户端**：一次 stream 恰好消费一次录制，
   没有网络也没有重试，重放的 Engine 因此可以在审计模式下运行。

## 不做

- **root thread id 从 Session id 派生**：会让「同一 Session id、同一 threads 目录、不同 Journal 根」的
  组合撞文件。派生自 root thread id 的标识进入上下文时（peer 消息的 `from_thread`）会分叉，作为边界记录。
- **重放 `CancelTurn` / `Shutdown`**：控制类提交的时机依赖运行时状态，录制里没有可靠的对应点。
- **比较重放与录制的 Journal**：留给读 Journal 的工具。

## 影响

- R1：无业务概念。
- R2–R4：重放的 Engine 与普通 Engine 无异。
- R5：无影响。

### 行为变化

- `spawn_skill` 工具发起的派发句柄不再随机（`sp_<hash>`）；直接调 `engine.spawn_skill()` 的行为不变，
  多一个可选的 `handle_id` 参数。
- `AttemptObservableClientAdapter` 接受 `JournalReplayClient`。

## 验证

`tests/loop/test_journal_replay_session.py`（3 项）：含工具调用与派发的对话整条重放、改动输入后第一步分叉、
录制里的挂起在重放里不再停下。全量 `pytest tests/` 通过；ruff 门禁与 `mypy src/` 清零。
