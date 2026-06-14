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

### 决策三:同步钩子 vs 异步事件 —— 取同步(本 turn 收尾的确定性一步)

> **⚠️ 见文末「修正(2026-06-14)」——本节初稿把顺序保证表述过强,实际边界以修正为准。**

钩子与事件的分界,在于内核是否**消费返回值 / 同步介入状态机**:`turn_completed` 事件 fire-and-forget;`PostTurn` 同步钩子是 **turn N 收尾的同步一步**——在状态回写之后、turn N 自己的 task 结束之前确定性执行,且能看到本 turn 已落定的 history(测试 `test_post_turn_fires_after_state_writeback` 钉死)。

codex ReviewTask 与 hermes background_review **其实都偏异步**,削弱「必须同步」的论据。本 change 仍取同步钩子,因为它把 review/固化作为 turn 收尾的**确定性一步**(而非 fire-and-forget 旁路),且能在本 turn 状态落定后立即介入。R4 经 `HookContext.extras["cancel"]` 把本 turn CancellationToken 交给钩子,长耗时可中断,宿主重活应自行 detached。

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

## 修正(2026-06-14):顺序保证边界澄清

本 ADR 初稿(决策三/背景/备选)把顺序保证表述为「post_turn 在**下一 turn 启动之前**完成」「等宿主跑完才**放行下一 turn**」。复核引擎代码后**纠正**:

- 引擎以 `asyncio.create_task(self._run_turn_for(...))` 派发 turn,**不串行化相邻 turn**(`_run_turn_for` 仅有"活跃挂起"守卫,无"单活跃 turn"锁)。
- 故 post_turn 实际保证的是 **「本 turn 收尾内、状态回写之后」**(turn N 自己的 task 内),**不是**「任何下一 turn 启动之前」。
- 且 `turn_completed` 在 post_turn **之前** emit;宿主若在 `turn_completed` 后并发提交下一 turn,该 turn 可与 post_turn 交错。

**实际可用的跨 turn 顺序**:宿主须**等 `post_turn_hook_fired` 再提交下一轮**(而非等 `turn_completed`)。

**决策不变**(仍取同步 post_turn 钩子,作为 turn 收尾的确定性一步);仅澄清其保证边界。契约措辞已同步更新 `capabilities/hooks.md` / `agent-loop.md` / `configurable-knobs.md`;新增测试 `test_post_turn_fires_after_state_writeback` 钉死真实保证(回写之后触发);demo `examples/post_turn_review/` 改为等 `post_turn_hook_fired` 演示跨 turn 顺序(替代原 sleep)。
