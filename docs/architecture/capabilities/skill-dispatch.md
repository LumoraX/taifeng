# Capability: skill-dispatch

## Purpose

Composite skill 通过 `call_skill` 派发到子 skill；静态 + 动态环检测；深度限制；白名单；call_skill 完整生命周期含 Hook + PermissionPolicy 双重门控。
## Requirements
### Requirement: call_skill 完整生命周期含 Permission + Hook

LLM 调用 `call_skill(target_skill_id, args, reason?)` 工具时，系统 SHALL 按以下顺序执行：

1. `DispatchPolicy.check`（既有：白名单 / 深度 / 环检测）—— 失败立即返回 `ToolResult.error("dispatch_rejected: {verdict.reason}")`，**不**进入后续步骤
2. `HookRunner.run("pre_skill_dispatch", PreSkillDispatchHook(...))` —— 任一 hook deny → 返回 `ToolResult.error("skill_dispatch_hook_denied: {reason}")` + emit `skill_dispatch_hook_denied`
3. `PermissionPolicy.check(PermissionRequest.for_skill_dispatch(..., reason=arguments.get("reason", "")))` —— deny → 返回 `ToolResult.error("skill_dispatch_denied: {reason}")` + emit `skill_dispatch_permission_denied`（含 `request_reason` 字段）
4. push call_stack，构造子 ToolContext，调 `dispatcher.run_sub_skill(...)`
5. 子 turn 完成后 `HookRunner.run("post_skill_dispatch", PostSkillDispatchHook(...))` —— hook 决策**仅用于审计**，不影响 ToolResult
6. 返回 ToolResult.ok / error 含 `sub_thread_id / final_text / duration_ms`
7. `skill_dispatched` EventMsg 的 `data` SHALL 含 `reason` 字段

#### Scenario: DispatchPolicy 通过但 PermissionPolicy 拒绝
- **WHEN** target 在 child_skills 白名单内，但 `PermissionRule(scope='skill_dispatch', target='oncology-deep-analysis', mode='deny')` 命中
- **THEN** ToolResult.error("skill_dispatch_denied: ...")
- **AND** SHALL NOT 启动子 TurnRunner
- **AND** 父 turn 继续，LLM 收到 error 决定后续动作

#### Scenario: PreDispatch hook 拒绝（free tier 黑名单）
- **WHEN** 注册 hook 对 free tier 用户的所有 dispatch 返回 deny
- **THEN** PermissionPolicy SHALL NOT 被调用；ToolResult.error 含 hook reason
- **AND** EventMsg `skill_dispatch_hook_denied` 被发出

#### Scenario: PostDispatch hook 仅审计
- **WHEN** post_skill_dispatch hook 返回 deny
- **THEN** ToolResult SHALL NOT 被修改（仍是 sub_turn 实际结果）
- **AND** EventMsg 显示 hook 已记录决策但不阻断

#### Scenario: 上下文字段透传到 PermissionRequest
- **WHEN** 父 entry skill A → composite B → composite C，dispatch C 调用 D
- **THEN** PermissionRequest SHALL 含：
  - `call_chain = ("A", "B", "C")`
  - PreSkillDispatchHook 的 `caller_skill_id = "C"` / `target_skill_id = "D"`
  - `entry_skill_id = "A"` / `turn_index = <parent turn 序号>`
  - `reason = <arguments.get("reason", "")>`

### Requirement: PermissionPolicy=None 时跳过（向后兼容）

`ctx.extras["permission_policy"]` 是 None 时，步骤 3 SHALL 跳过（DispatchPolicy + Hook 仍生效）。业务侧老代码（未传 permission_policy）SHALL 行为不变。

#### Scenario: 老业务不传 permission_policy
- **WHEN** EnginePool 构造时未注入 PermissionPolicy
- **THEN** call_skill SHALL 仅走 DispatchPolicy + Hook，跳过 step 3
- **AND** 不发 `skill_dispatch_permission_denied`（因为没检查）

### Requirement: DispatchPolicy 暴露 subagent_approval_mode 字段

`DispatchPolicy` SHALL 新增 keyword-only 字段：

```python
subagent_approval_mode: Literal["inherit", "auto_deny", "auto_allow"] = "inherit"
```

- 默认 `"inherit"`：保持改造前行为（透传父 permission_policy 到子 TurnRunner）
- `"auto_deny"`：子 turn 内任何 PermissionPolicy `ask` 决策自动转 `deny`（不调 prompter）
- `"auto_allow"`：子 turn 内任何 PermissionPolicy `ask` 决策自动转 `allow`（不调 prompter）

字段类型为 Literal 字符串；非合法值 SHALL 在 `__post_init__` 抛 `ValueError`。

#### Scenario: 默认值是 inherit
- **WHEN** `DispatchPolicy()` 构造
- **THEN** `subagent_approval_mode` SHALL 等于 `"inherit"`

#### Scenario: 非法值抛 ValueError
- **WHEN** `DispatchPolicy(subagent_approval_mode="strict")` 构造
- **THEN** SHALL 抛 `ValueError`，message 含 "subagent_approval_mode"

### Requirement: 子 TurnRunner 根据 mode 决定 permission_policy 处理

`TurnRunner.run_sub_skill` SHALL 根据 `self.dispatch_policy.subagent_approval_mode` 决定子 TurnRunner 收到的 permission_policy：

- `inherit` + 父 policy 非 None → 子 turn `permission_policy = self.permission_policy`（同对象引用）
- `inherit` + 父 policy 是 None → 子 turn `permission_policy = None`
- `auto_deny` + 父 policy 非 None → 子 turn `permission_policy = _SubagentAutoDecisionPolicy(inner=self.permission_policy, fallback="deny")`
- `auto_deny` + 父 policy 是 None → 子 turn `permission_policy = None`（无 policy 可包装；不引入额外门控）
- `auto_allow` + 父 policy 非 None → 子 turn `permission_policy = _SubagentAutoDecisionPolicy(inner=self.permission_policy, fallback="allow")`
- `auto_allow` + 父 policy 是 None → 子 turn `permission_policy = None`

#### Scenario: inherit 模式直接透传（不包装）
- **WHEN** `DispatchPolicy(subagent_approval_mode="inherit")`
- **AND** 父 turn 触发 call_skill 派发
- **THEN** 子 TurnRunner.permission_policy SHALL `is` 父 TurnRunner.permission_policy（同对象）

#### Scenario: auto_deny 模式包装父 policy
- **WHEN** `DispatchPolicy(subagent_approval_mode="auto_deny")`
- **AND** 父 turn 触发派发
- **THEN** 子 TurnRunner.permission_policy SHALL 是 `_SubagentAutoDecisionPolicy(inner=父, fallback="deny")`
- **AND** 子 turn 内 `ask` 决策 SHALL 不调 prompter

#### Scenario: 父 policy 为 None 时不包装
- **WHEN** 父 TurnRunner.permission_policy=None
- **AND** `DispatchPolicy.subagent_approval_mode="auto_deny"`
- **THEN** 子 TurnRunner.permission_policy SHALL 为 None
- **AND** SHALL NOT 创建 `_SubagentAutoDecisionPolicy` 实例

### Requirement: `_SubagentAutoDecisionPolicy` 复用 inner rules 但跳过 prompter

`_SubagentAutoDecisionPolicy.check(request)` SHALL：

1. 遍历 `self.inner.rules`，找到第一个 `rule.matches(request)` 的 rule
2. 命中 rule.mode == "allow" → 返回 `PermissionDecision.allow(reason=rule.reason or "subagent_rule_allow")`
3. 命中 rule.mode == "deny" → 返回 `PermissionDecision.deny(reason=rule.reason or "subagent_rule_deny")`
4. 命中 rule.mode == "ask" **或** 全不命中且 `inner.default_mode == "ask"` →
   - fallback="allow" → 返回 `PermissionDecision.allow(reason="subagent_auto_allow")`
   - fallback="deny" → 返回 `PermissionDecision.deny(reason="subagent_auto_deny")`
5. 全不命中且 `inner.default_mode == "allow"` → 返回 `PermissionDecision.allow(reason="inner_default_allow")`
6. 全不命中且 `inner.default_mode == "deny"` → 返回 `PermissionDecision.deny(reason="inner_default_deny")`

包装类 SHALL **不**调用 `inner.prompter`（即使 inner.prompter 非 None）。

#### Scenario: rule.allow 命中走 allow
- **WHEN** inner.rules 含 `(scope='tool_use', target='*', mode='allow')` 命中 request
- **AND** wrapper fallback="deny"
- **THEN** decision SHALL `granted=True, mode="allow"`

#### Scenario: rule.ask 命中 + fallback=deny → deny（关键场景）
- **WHEN** inner.rules 含 `(scope='shell_exec', target='ls *', mode='ask')` 命中
- **AND** wrapper fallback="deny"
- **AND** inner.prompter 非 None（mock）
- **THEN** decision SHALL `granted=False, mode="deny", reason="subagent_auto_deny"`
- **AND** mock prompter SHALL NOT 被调用

#### Scenario: 无 rule + default_mode=ask + fallback=allow → allow
- **WHEN** inner.rules 空
- **AND** inner.default_mode == "ask"
- **AND** wrapper fallback="allow"
- **THEN** decision SHALL `granted=True, reason="subagent_auto_allow"`

#### Scenario: 无 rule + default_mode=allow → allow（不被 fallback 覆盖）
- **WHEN** inner.rules 空
- **AND** inner.default_mode == "allow"
- **AND** wrapper fallback="deny"
- **THEN** decision SHALL `granted=True, reason="inner_default_allow"`（inner 的明确决策优先）

### Requirement: Emit subagent_policy_overridden event 当 wrapper 创建

`TurnRunner.run_sub_skill` SHALL 在创建 `_SubagentAutoDecisionPolicy` 包装时 emit `subagent_policy_overridden` EventMsg：

- `data["target_skill_id"]` = 子 skill id
- `data["mode"]` = `"auto_deny"` 或 `"auto_allow"`
- `data["depth"]` = 派发后栈深度（= parent_stack.depth + 1）

inherit 模式 SHALL NOT emit 该事件。

#### Scenario: inherit 模式不 emit
- **WHEN** `DispatchPolicy.subagent_approval_mode="inherit"`
- **AND** 子 skill 派发
- **THEN** SHALL NOT 出现 `subagent_policy_overridden` event

#### Scenario: auto_deny 模式 emit 一次
- **WHEN** `DispatchPolicy.subagent_approval_mode="auto_deny"`
- **AND** 父 policy 非 None
- **AND** 子 skill 派发
- **THEN** SHALL emit 恰好 1 个 `subagent_policy_overridden`，data.mode == "auto_deny"

#### Scenario: 父 policy 为 None 时不 emit（无包装）
- **WHEN** `DispatchPolicy.subagent_approval_mode="auto_deny"`
- **AND** 父 TurnRunner.permission_policy=None
- **AND** 子 skill 派发
- **THEN** SHALL NOT emit `subagent_policy_overridden`（因为没创建包装）

### Requirement: call_skill 工具 schema 强制 `reason` 字段

> **AMENDMENT 2026-05-27**：本 Requirement 自归档后修订 ——
> `reason` 字段从 optional 改为 **required**。原因：实践表明 LLM 对可选字段
> 的填充率远低于预期，导致 HITL 审批方拿不到决策依据。改 required 后
> 主流 provider (gpt-4o / gemini-2.x) 会严格执行 schema 强制填入。

`call_skill` 工具的 `input_schema.properties` SHALL 包含 `reason` 字段：

- `type: "string"`
- **required**：`reason` SHALL 出现在 `input_schema.required` 列表中
- `description` SHALL 引导 LLM 提供 1-2 句话说明派发动机，并明确"缺乏有意义的 reason 可能导致审批方直接拒绝派发"

`make_call_skill_tool()` 返回的 `ToolSpec.description` SHALL 包含命令式提示"**调用时务必附带 reason 字段**"+ 具体好例 / 反例，让 LLM 在工具选择阶段就理解该字段的价值。

**Handler 防御性兜底**：尽管 schema required，handler 内部 SHALL 仍用 `args.get("reason", "")` 提取（默认空串）—— 用于绕过 schema 校验的直调路径（如 unit test）不抛 KeyError。

#### Scenario: LLM 通过 schema 校验时必填 reason
- **WHEN** LLM 调 `call_skill({"skill_id": "X"})`（未传 reason）
- **THEN** provider 的 schema 校验层 SHALL 拒绝该 tool call（具体错误形态由 provider 决定）

#### Scenario: handler 直调路径兼容 reason 缺失
- **WHEN** 测试代码直接 `tool.handler({"skill_id": "X"}, ctx)` 绕过 schema 校验
- **THEN** handler SHALL 不抛 KeyError；`PermissionRequest.reason` 为空串 `""`
- **AND** 工具流程正常完成（防御性兜底保护非 schema 路径）

#### Scenario: LLM 提供 reason
- **WHEN** LLM 调 `call_skill({"skill_id": "security-reviewer", "reason": "需要审查这段 SQL 拼接代码的注入风险"})`
- **THEN** `PermissionRequest.reason == "需要审查这段 SQL 拼接代码的注入风险"`
- **AND** 业务侧 PermissionPrompter 的 `prompt(request)` 收到该值
- **AND** `skill_dispatched.data.reason` 同样为该字符串

#### Scenario: schema 校验拒绝错误类型
- **WHEN** LLM 调 `call_skill({"skill_id": "X", "reason": 123})`（reason 不是 string）
- **THEN** 工具 SHALL 在 schema 校验阶段拒绝（既有 schema 校验链路）
- **AND** ToolResult.error 含 schema 校验失败原因

### Requirement: reason 流转到 PermissionRequest + EventMsg + 持久化

`_call_skill_handler` SHALL 在生命周期阶段 3（PermissionPolicy 检查）之前从 `arguments` 提取 `reason`（缺省 `""`），并：

1. 透传给 `PermissionRequest.for_skill_dispatch(..., reason=reason)`
2. 写入 `skill_dispatched` EventMsg 的 `data["reason"]` 字段
3. 写入 `skill_dispatch_permission_denied` EventMsg 的 `data["request_reason"]` 字段（与 policy 的 `decision.reason` 区分）

JSONL 持久化（既有路径）SHALL 通过 tool_call arguments 的整体存储自动包含 reason —— 无需新增字段。

#### Scenario: PermissionPrompter 在 HITL UI 上能看到 reason
- **WHEN** LLM 调 `call_skill({"skill_id": "X", "reason": "Y"})` 且 PermissionPolicy 返回 ask
- **THEN** prompter 收到的 `PermissionRequest.reason == "Y"`
- **AND** business 侧的 CallbackPrompter / McpPrompter / 自定义 prompter 一律自动收到该值

#### Scenario: EventMsg 订阅者收到 reason
- **WHEN** 订阅 `skill_dispatched` 事件
- **THEN** `data["reason"]` 字段存在（空串或 LLM 提供的字符串）

#### Scenario: 拒绝路径的 audit
- **WHEN** call_skill 派发被 PermissionPolicy deny
- **THEN** `skill_dispatch_permission_denied.data["request_reason"]` SHALL 含 LLM 自陈的 reason
- **AND** `skill_dispatch_permission_denied.data["reason"]` SHALL 含 policy 的 decision.reason（既有字段名）

## 数据契约

### `SkillDefinition.type ∈ {"atomic", "composite"}`
- atomic: 不可作为入口，不可声明 child_skills / tool_names
- composite: 必须声明 child_skills（>=1）；可作为 entry（``entry: true``）

### `CallStack`（不可变值对象）
- frames[0] 始终是 entry skill 的栈底帧
- push 返回新 CallStack（保留旧引用安全）
- contains(skill_id) 用于环检测
- path() 返回 skill_id 序列

### `DispatchVerdict`
- `allowed: bool`
- `reason ∈ {unknown_skill, max_depth_exceeded, cycle_detected, not_in_whitelist, cannot_call_entry_skill}`
- `path: tuple[str, ...]` —— 失败时给出完整调用路径

## 行为契约

### Requirement: 静态环检测在加载期触发
**WHEN** `FilesystemSkillRegistry.load(skills_dir)` 扫描完成
**THEN** 系统 SHALL 对 `skill.child_skills` 构建有向图并运行 DFS 环检测
**AND** 任一环存在时 SHALL 抛 `CircularSkillReference`，进程拒绝启动
**AND** 错误消息 SHALL 包含完整环路径（`a → b → c → a`）

#### Scenario: 自引用环
- **WHEN** skill `a` 的 child_skills 含 `a`
- **THEN** 加载失败，错误路径 `[a, a]`

#### Scenario: 三跳环
- **WHEN** `a → b → c → a`
- **THEN** 加载失败，错误路径包含 a, b, c

### Requirement: 派发期白名单校验
**WHEN** `call_skill(skill_id, args)` 工具被 LLM 触发
**THEN** `DispatchPolicy.check` SHALL 校验：
1. target skill 存在于 snapshot
2. target.id ∈ caller.child_skills
3. !target.entry
4. !stack.contains(target.id)
5. stack.depth < caller.max_call_depth

#### Scenario: target 不在白名单
- **WHEN** caller.child_skills = {"a"}，调用 "b"
- **THEN** 返回 `DispatchVerdict.reject("not_in_whitelist")`

#### Scenario: target 是 entry skill
- **WHEN** 调用另一个 entry skill
- **THEN** 返回 `DispatchVerdict.reject("cannot_call_entry_skill")`

#### Scenario: 动态环
- **WHEN** stack 路径含 target.id（即使白名单允许）
- **THEN** 返回 `DispatchVerdict.reject("cycle_detected")` 含完整 path

### Requirement: 深度限制
**WHEN** stack.depth 达到 entry skill 的 max_call_depth
**THEN** 再次派发 SHALL 被拒绝 `max_depth_exceeded`
**AND** 默认 max_call_depth = 6

### Requirement: 子 turn 隔离
**WHEN** call_skill 派发成功
**THEN** 系统 SHALL 启动新的 TurnRunner 处理子 skill
**AND** 子 turn 与父 turn 共享 store / model_client / tool_runtime
**AND** 子 turn 的 history_buffer 独立（不串污）
**AND** 子 turn 的 thread_id 是新建的（source=`subskill:<parent_id>`）

### Requirement: 子 turn 结果回流
**WHEN** 子 TurnRunner 完成
**THEN** 系统 SHALL 把 outcome.final_text 作为 ToolResult.output 返回给父 turn
**AND** 子 turn 失败时 SHALL 返回 ToolResult.error，含子 thread_id 便于追溯
**AND** 父 turn 收到结果后通过 EventMsg.SkillReturned 通知订阅者

## 边界与错误处理

- skill_id 未知：返回 `unknown_skill`，不抛异常
- args 不是 dict：返回 `bad_args`
- 子 turn 抛异常：返回 `sub_skill_failed`，父 turn 继续运行
- 取消传播：父 turn 的 CancellationToken 取消时，子 turn 同步取消

## 不在范围（Non-goals）

- ❌ 跨进程 skill 派发（子 skill 在同进程同 actor）
- ❌ 跨 entry skill 派发（必须开新 session）
- ❌ skill 版本协商（snapshot 锁定）
- ❌ skill 间通信协议（除 call_skill 返回值外无 IPC）
