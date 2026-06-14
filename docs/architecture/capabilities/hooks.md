# hooks Specification

## Purpose
TBD - created by archiving change permission-gate-completeness. Update Purpose after archive.
## Requirements
### Requirement: HookKind literal 扩展

`HookKind` literal SHALL 包含：`"pre_tool_use" | "post_tool_use" | "pre_compact" | "pre_turn" | "pre_skill_dispatch" | "post_skill_dispatch"`。

`HookRegistry._handlers` dict SHALL 包含 6 个 key（新增两个初始为空 list）。现有 4 类 hook 行为 SHALL 不变。

#### Scenario: 注册新 hook
- **WHEN** 业务侧 `hook_runner.registry.register("pre_skill_dispatch", my_hook)`
- **THEN** SHALL 不报错；`hook_runner.registry.handlers("pre_skill_dispatch")` SHALL 包含 my_hook

#### Scenario: 旧 hook 不退化
- **WHEN** 业务侧只用 `pre_tool_use / post_tool_use / pre_compact / pre_turn`
- **THEN** 行为完全不变（既有测试不退化）

### Requirement: 新增 PreSkillDispatchHook / PostSkillDispatchHook 数据类

系统 SHALL 提供以下 frozen dataclass：

- `PreSkillDispatchHook`: `target_skill_id: str / args: dict[str, Any] / caller_skill_id: str / call_chain: tuple[str, ...] / depth: int`
- `PostSkillDispatchHook`: `target_skill_id: str / caller_skill_id: str / success: bool / duration_ms: int / sub_thread_id: str | None / output_preview: str`（output_preview 最多 1KB 截断）

#### Scenario: 顶层导入
- **WHEN** 业务侧 `from taifeng import PreSkillDispatchHook, PostSkillDispatchHook`
- **THEN** 两个符号 SHALL 全部可用

#### Scenario: hook data 不可变
- **WHEN** 代码尝试 `hook.target_skill_id = "x"`
- **THEN** SHALL raise `FrozenInstanceError`

### Requirement: PostSkillDispatch 在失败时也触发

子 TurnRunner 完成（成功或失败）时，`HookRunner.run("post_skill_dispatch", ...)` SHALL 都被调用。`PostSkillDispatchHook.success` 反映实际结果。Hook 异常 SHALL NOT 影响 ToolResult 返回（只发 telemetry）。

#### Scenario: 子 turn 失败仍触发 post hook
- **WHEN** 子 TurnRunner 抛 LLMError 失败退出
- **THEN** `post_skill_dispatch` hook SHALL 被调用，PostSkillDispatchHook.success = False
- **AND** ToolResult SHALL 是 error（反映子 turn 失败），但 hook 已执行

#### Scenario: post hook 自身抛异常
- **WHEN** 业务侧 post_skill_dispatch hook 抛 RuntimeError
- **THEN** SHALL 被 HookRunner 捕获，仅发 telemetry
- **AND** ToolResult SHALL NOT 被修改

### Requirement: `pre_turn` hook 调用点

`AgentEngine._run_turn_for` 在 pre_turn hook deny 时 SHALL emit `turn_failed` 事件，其 `data.kind` 字段值 SHALL 等于 `"pre_turn_hook_denied"`（与对应 event kind 字符串一致），而非任何不存在的异常类名。

本 Requirement 在 change `2026-05-25-hook-wiring-pre-compact-pre-turn` 中首次声明；本 change 仅订正其中一个 Scenario 的字段值描述。

#### Scenario: hook deny 阻断 turn（A3 fix）

- **WHEN** 业务侧注册 always-deny 的 `pre_turn` handler，submit `UserMessage(text="x")`
- **THEN** SHALL emit `pre_turn_hook_denied` 事件，`data` 含 `reason`
- **AND** SHALL emit `turn_failed` 事件，`error="pre_turn_hook_denied"`、~~`kind="HookDecisionDenied"`~~ **`kind="pre_turn_hook_denied"`**
- **AND** SHALL NOT emit `turn_started`
- **AND** TurnRunner SHALL NOT 被实例化（不进入主循环）

**理由**：既有约定（`InstructionFetchError` 等）`kind` 对应**真实抛出的异常类名**。hook deny 不抛异常（`HookDecision.deny()` 返回 dataclass），无对应 exception class。此场景下 `kind` 字段值改为与对应 event kind 一致的描述性 label，保持上游 telemetry 过滤一致性。

### Requirement: `post_turn` hook 调用点

`AgentEngine._build_and_run_runner` 在 `runner.run()` 返回、turn 状态(history /
cache_anchor / rewind 节点表 / 指纹 / token)回写之后,**SHALL** 对 **root turn 的真终态**
**同步**触发 `post_turn` hook —— 即 post_turn 是 **turn N 收尾的同步一步**(回写已发生、
在 turn N 自己的 task 结束之前)。与 `pre_turn` 作用域对称(仅 root turn;detached spawn /
call_skill 子 turn 不触发,其收尾审计由 `post_skill_dispatch` 覆盖)。

**顺序保证的精确边界(重要)**:引擎以 `create_task` 派发 turn、**不串行化相邻 turn**
(`_run_turn_for` 仅有"活跃挂起"守卫,无"单活跃 turn"锁)。因此 post_turn 保证的是
**「本 turn 收尾内、回写之后」**,**不是**「任何下一 turn 启动之前」——若宿主在
`turn_completed`(它在 post_turn **之前** emit)后立即并发提交下一 turn,该 turn 可与
post_turn 在事件循环上交错。**要跨 turn 顺序**(下一轮基于本轮固化结果),宿主须
**等 `post_turn_hook_fired` 再提交下一轮**(而非等 `turn_completed`)。

触发门控:`TurnOutcome.end_reason ∈ {"suspended", "cancelled"}` 时 **SHALL NOT** 触发
——挂起是暂停等 Resume(续跑到真终态时才触发),取消是 teardown。其余终态(completed /
max_iterations / resource_limit_exceeded / denial_circuit_open / error)**SHALL** 触发。

`post_turn` 为**审计型**(对齐 `post_skill_dispatch`):经 `HookRunner.run_audit_only`
触发,返回 `deny` 或抛异常都 **SHALL NOT** 改变已终结的 turn(仅写日志)。仅当注册了
`post_turn` handler 时才执行(常见路径零开销)。

`PostTurnHook` 数据载荷 **SHALL** 包含(全部取自 `TurnOutcome`,零额外快照成本):
- `end_reason: str`
- `success: bool`
- `final_text: str`(本 turn 最终答案;需全量 items 时宿主自调 `engine.history_snapshot()`)
- `iteration: int`(本 turn 的 index,= 同 turn `pre_turn` 的 iteration)

**R4 可取消**:hook 执行经 `HookContext.extras["cancel"]` 拿到本 turn 的
CancellationToken。post_turn 同步执行会占用本 turn 收尾时段,长耗时 hook 须可被中断
(宿主重活应自行 detached)。**定位**:这是「自我 review / 记忆固化」等规则② 认知回路的
内核 seam —— 内核只开口子,review 内容(审什么 / 学什么 / 存哪)全留 userspace
(`spawn_skill` + 工具白名单 + `memory_store.writeback`),不入内核(R1)。

#### Scenario: completed turn 触发 post_turn
- **WHEN** 一个 root turn 以 `end_reason="completed"` 结束,且注册了 `post_turn` handler
- **THEN** `post_turn` hook SHALL 被触发一次,入参 `success=True` / `final_text` / `iteration`
- **AND** 触发时本 turn 的 user_message 与 assistant_message SHALL 已回写进 `engine.history`(收尾的同步一步,回写之后)
- **AND** SHALL emit `post_turn_hook_fired` 事件(在 `turn_completed` 之后)

#### Scenario: 挂起不触发,resume 跑到终态才触发
- **WHEN** 一个 root turn 以 `end_reason="suspended"` 暂停
- **THEN** `post_turn` hook SHALL NOT 在此刻触发
- **WHEN** 该 turn 后续经 Resume 续跑以 `end_reason="completed"` 结束
- **THEN** `post_turn` hook SHALL 在此刻触发一次

#### Scenario: cancelled 不触发
- **WHEN** 一个 root turn 以 `end_reason="cancelled"` 结束
- **THEN** `post_turn` hook SHALL NOT 触发

#### Scenario: 子 turn 不触发 root 级 post_turn
- **WHEN** root turn 内经 call_skill 派发的子 turn 完成
- **THEN** 该子 turn 完成 SHALL NOT 触发 `post_turn`;仅外层 root turn 抵达真终态时触发一次

#### Scenario: 审计型不可否决
- **WHEN** `post_turn` handler 返回 `deny` 或抛异常
- **THEN** 已终结的 turn 结果 SHALL NOT 被修改(无 `turn_failed`、无回滚)

#### Scenario: 无 handler 零开销
- **WHEN** 未注册任何 `post_turn` handler
- **THEN** SHALL NOT emit `post_turn_hook_fired`(不进入 hook 执行路径)

### Requirement: `pre_compact` hook 调用点

`TurnRunner._maybe_compress` 在 budget 阈值判断通过后、`CompressionOrchestrator.maybe_compress` 调用前，**SHALL** 执行 `pre_compact` hook。具体顺序：

1. 估算 `tokens = estimate_history_tokens(history_buffer)`
2. `if not force:` 阈值判断（既有逻辑）
3. `HookRunner.run("pre_compact", PreCompactHook(phase, token_estimate, history_length), HookContext(...))`
4. **WHEN** decision.allow == True → 继续 emit `compaction_started` + 跑 strategy
5. **WHEN** decision.allow == False → emit `pre_compact_hook_skipped`，跳过本轮压缩、turn 继续

`PreCompactHook` 数据载荷 **SHALL** 包含：
- `phase: Literal["pre_turn", "mid_turn", "manual"]`
- `token_estimate: int`
- `history_length: int`

#### Scenario: pre_compact deny 跳过压缩
- **WHEN** 业务侧注册 always-deny 的 `pre_compact` handler，且 token 超过 `budget.soft_limit_ratio`
- **THEN** SHALL emit `pre_compact_hook_skipped` 事件，`data` 含 `phase` / `reason` / `token_estimate`
- **AND** SHALL NOT emit `compaction_started` / `compaction_completed`
- **AND** `history_buffer` 长度 SHALL 保持不变
- **AND** `cache_anchor_index` SHALL 保持不变（R2 cache-aware）
- **AND** turn 主循环 SHALL 继续（不报错、不终止）

#### Scenario: pre_compact allow 走原压缩路径
- **WHEN** 业务侧注册 always-allow 的 `pre_compact` handler，且 token 超过 soft limit
- **THEN** SHALL emit `compaction_started`，并按既有逻辑走 strategy
- **AND** SHALL NOT emit `pre_compact_hook_skipped`

#### Scenario: 多 phase 都触发 hook
- **WHEN** 一个 turn 内分别进入 `phase="pre_turn"` / `phase="mid_turn"`
- **THEN** `pre_compact` hook SHALL 被调用 2 次（每个 phase 一次）
- **AND** `PreCompactHook.phase` 字段 SHALL 准确反映触发阶段

#### Scenario: manual 压缩也走 hook
- **WHEN** 业务侧 `engine.submit(CompactNow(force=True))` 触发 manual 压缩
- **THEN** `pre_compact` hook SHALL 被调用，`phase="manual"`
- **AND** hook deny 时 SHALL 跳过本次 manual 压缩（业务可拒绝管理员触发的 compact）

### Requirement: 新 EventMsg 子类暴露

[src/taifeng/loop/event.py](../../../src/taifeng/loop/event.py) **SHALL** 提供：

- `PreTurnHookDenied(_Msg)` —— `kind = "pre_turn_hook_denied"`，`data` 典型字段 `{ "reason": str, "user_text_preview": str }`
- `PreCompactHookSkipped(_Msg)` —— `kind = "pre_compact_hook_skipped"`，`data` 典型字段 `{ "phase": str, "reason": str, "token_estimate": int }`

两个子类 **SHALL** 加入 `EventMsg.msg` 的 discriminator union，使得 `EventMsg(submission_id=..., msg=PreTurnHookDenied(...))` 通过 pydantic 校验。

#### Scenario: pydantic 反序列化
- **WHEN** 业务侧 `EventMsg.model_validate_json(json_str)` 反序列化含 `"kind": "pre_turn_hook_denied"` 的 JSON
- **THEN** SHALL 得到 `EventMsg(msg=PreTurnHookDenied(...))` 实例，不抛 ValidationError

#### Scenario: MsgKind literal 完整性
- **WHEN** 静态检查 `MsgKind` literal
- **THEN** SHALL 包含 `"pre_turn_hook_denied"` 与 `"pre_compact_hook_skipped"`

### Requirement: HookRegistry 桶位完整性（无死代码）

`HookRegistry._handlers` dict SHALL 包含 9 个 kind 桶位，且所有 9 个桶位 SHALL 在 `src/taifeng/` 内有至少一个调用点（即不存在"声明但未触发"的死代码）。

具体调用点映射（实现层文档，spec 只约束契约）：

- `pre_tool_use` → `loop/turn.py::_sample_once`：deny → tool 不执行，返回 hook_denied error
- `post_tool_use` → `loop/turn.py::_sample_once`：仅审计 run_audit_only
- `pre_compact` → `loop/turn.py::_maybe_compress`：deny → 跳过压缩
- `pre_turn` → `loop/engine.py::_run_turn_for`：deny → emit turn_failed
- `post_turn` → `loop/engine.py::_fire_post_turn_hook`（`_build_and_run_runner` 收尾）：仅审计 run_audit_only；root turn 真终态触发，emit post_turn_hook_fired
- `pre_skill_dispatch` → `tool/builtins/call_skill.py`：deny → call_skill 返回 hook_denied error
- `post_skill_dispatch` → `tool/builtins/call_skill.py`：仅审计 run_audit_only
- `pre_script_use` → `tool/builtins/run_script.py`：deny → run_script 返回 hook_denied error
- `post_script_use` → `tool/builtins/run_script.py`：仅审计 run_audit_only

#### Scenario: 9 个 hook kind 都有调用点
- **WHEN** 静态扫描 `grep -rn 'hooks.run\|hook_runner.run\|run_audit_only' src/`
- **THEN** SHALL 至少出现以下 9 类 hook kind 的调用：上表 9 项全覆盖

