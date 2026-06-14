# ADR 0019: PostTurn 钩子 —— turn 终态收尾审计 seam

- 状态：Accepted
- 日期：2026-06-14
- 关系：对称扩展现有 hooks 家族(`pre_turn` 等);立项依据 ADR 0017 规则②(模型认知回路原语：自我 review)

## 背景

2026-06-14 对照 5 个参照实现(codex / claw-code / openclaw / hermes / opencode)做差距分析时,**codex(`session/review.rs` ReviewTask)与 hermes(`background_review.py`)两个独立来源都收敛到「turn 结束后让模型对自己刚做的事做结构化自审 / 经验固化」**这一机制。ADR 0017 规则②把「自我 review」明列为该做的内核认知回路原语。

但要分清**机制内核**与**产品逻辑**:codex / hermes 的 review 实现都把「审什么 prompt / 学到的东西存哪(memory 命名空间 / skill 库)/ 凭据继承」焊进去——这些是产品逻辑(规则④,触 R1 业务侵入)。剥掉后,内核真正缺的只有一个 seam:**turn 进入终态时、宿主可确定性介入的钩子点**。

核实现状:taifeng hooks 家族有 `PreTurn`(turn 准入闸门,可否决)却**无对称的 turn 终态钩子**。宿主想在 turn 收尾做「自省 → 固化记忆 → 再继续」这类**需要顺序保证**的认知回路,只能靠 `subscribe_all` 旁听 `turn_completed` 事件——而事件是 fire-and-forget、在 engine turn 状态机**之外**,无法保证「下一 turn 启动前」完成。

## 决策

### 决策一:补 `PostTurn` 钩子(对称 `PreTurn`,审计型)

`HookKind` 增 `post_turn`;`AgentEngine._build_and_run_runner` 在 `runner.run()` 返回、turn 状态回写之后、下一 turn 启动之前同步触发(新 helper `_fire_post_turn_hook`)。审计型不可否决(经 `run_audit_only`,deny / 异常仅写日志),对齐 `post_skill_dispatch`。入参 `PostTurnHook{end_reason, success, final_text, iteration}`,全取自 `TurnOutcome`(零额外快照成本;全量 items 宿主自调 `history_snapshot()`)。R3 新增 `post_turn_hook_fired` 事件。

### 决策二:仅 root turn、仅真终态触发

作用域与 `PreTurn` 对称——只对 root turn(`_build_and_run_runner` 被用户消息 / resume / rewind 三路径共用,天然覆盖 resume 续跑完);detached spawn / call_skill 子 turn 不触发,其收尾审计由 `post_skill_dispatch` 覆盖。

门控:`end_reason ∈ {suspended, cancelled}` **不触发**——挂起是暂停等 Resume(续跑到真终态时才触发),取消是 teardown(此刻触发与 R4 可取消语义矛盾)。其余终态(completed / max_iterations / resource_limit_exceeded / denial_circuit_open / error)均触发。

### 决策三:同步钩子 vs 异步事件 —— 取同步(顺序保证)

钩子与事件的分界,在于内核是否**消费返回值 / 同步暂停状态机**:`turn_completed` 事件 fire-and-forget,给不了「下一 turn 前完成」的顺序保证;`PostTurn` 同步钩子在 turn 边界内暂停、等宿主跑完才放行下一 turn。

需要权衡的是:codex ReviewTask 与 hermes background_review **其实都偏异步**,削弱「必须同步」的论据。但本 change 的目标场景(系统驱动、确定性、必须在下一轮前完成的自动复查 / 记忆固化)**要求顺序保证**——故取同步钩子。代价是钩子内重活会阻塞下一 turn(用户所求);缓解:R4 经 `HookContext.extras["cancel"]` 把本 turn CancellationToken 交给钩子,长耗时可中断,宿主重活应自行 detached。

### 决策四:review 执行逻辑全留 userspace(R1)

内核只提供 seam,**不内置 review 子系统**(那违反 R1 / 规则④)。review 的 prompt、审什么、学到的存哪,宿主用已有的 `spawn_skill`(detached)+ SKILL.md `tool_names` 白名单 + `memory_store.writeback` 自行拼「自省 → 固化」回路。内核零业务概念。

## 影响

- `src/taifeng/hooks/types.py`:`HookKind` 加 `post_turn` + `HookRegistry` 槽 + `PostTurnHook` dataclass;`hooks/__init__.py` 导出。
- `src/taifeng/loop/event.py`:`PostTurnHookFired` 事件(R3)。
- `src/taifeng/loop/engine.py`:`_build_and_run_runner` 接住 `outcome`,`_fire_post_turn_hook` 按 end_reason 把门触发。
- R1:内核只开 seam,review 内容留 userspace,零业务概念;R3:新增 `post_turn_hook_fired`;R4:hook 接 cancel token;R2/R5:钩子在 history/cache 回写**之后**触发,不动 history 内容、不动 cache anchor、JSONL append-only 不变。
- 活文档:`docs/architecture/capabilities/hooks.md`(8 → 9 kinds)、`docs/configurable-knobs.md` hooks 表。

## 备选(未采纳)

- **不加钩子,只补 userspace 范式文档(用 `subscribe_all` + `spawn_skill` 拼异步 review)**:若宿主可接受异步 review,事件订阅已够。未采纳——目标场景要求「下一轮前完成」的顺序保证,事件给不了。
- **做成 `review` Op / 内置 reviewer 子 agent**:把 review 语义焊进内核,违反 R1 / 规则④。未采纳。
- **可否决的 PostTurn**:turn 已终结无可拦,且与「审计型」语义冲突。未采纳。
