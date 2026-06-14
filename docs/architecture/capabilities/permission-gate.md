# permission-gate Specification

## Purpose
TBD - created by archiving change permission-gate-completeness. Update Purpose after archive.
## Requirements
### Requirement: PermissionRequest typed 字段 + 向后兼容

`PermissionRequest` SHALL 是 frozen pydantic BaseModel，含以下字段：

- `scope: Literal["tool_use", "script_exec", "skill_dispatch"]` —— required
- `target: str` —— required（工具名 / 脚本名 / skill_id）
- `reason: str = ""` —— 可选，LLM 自陈意图
- `metadata: dict[str, Any] | None = None` —— **若 scope ∈ ("tool_use", "script_exec")，SHALL 含 `metadata["args"]` = 该工具/脚本被调用的实际 args 字典**（供 PermissionRule.args_match 使用）；skill_dispatch scope 不强制
- `thread_id` / `submission_id` / `entry_skill_id` / `turn_index`：上下文必填
- `call_chain: tuple[str, ...]` —— 调用栈

业务侧领域上下文（租户 / 用户 / audience 等）SHALL 走开放的 `metadata` dict 透传——
引擎不引入任何业务命名字段（R1 业务零侵入）；taifeng 不解析 `metadata` 的 keys。

向后兼容承诺：本 change 不改 PermissionRequest 字段；仅说明 `metadata["args"]` 已经存在（工厂 `for_tool_call` / `for_script_exec` 一直在塞），现在被 args_match 真正用上。

#### Scenario: tool_use scope 必含 args metadata
- **WHEN** `PermissionRequest.for_tool_call("shell_exec", {"cmd": "ls"}, ...)`
- **THEN** `req.metadata["args"] == {"cmd": "ls"}`
- **AND** `PermissionRule(args_match={"cmd": "ls"})` 可以命中该 request

#### Scenario: skill_dispatch scope 不强制 args
- **WHEN** `PermissionRequest.for_skill_dispatch("backend-reviewer", ...)`
- **THEN** `req.metadata` 含 `caller_skill_id` 但不一定含 `args`
- **AND** 带 args_match 的 rule SHALL 不命中（args_match 非空 + args 缺 → 不命中）

<!-- 注：内核不持久化权限决策（不自动写回 rules）；业务侧若要 session/DB 级
     "记住一次批准"，在自己的 prompter 包装层读 decision.remember_until 后自管。 -->

### Requirement: 工厂方法强制 schema

`PermissionRequest` SHALL 提供以下工厂方法（required keyword-only 强制 schema）：

- `PermissionRequest.for_tool_call(tool_name, args, *, thread_id, submission_id, entry_skill_id, turn_index, extra_metadata=None)`
- `PermissionRequest.for_script_exec(script_name, args, *, thread_id, submission_id, entry_skill_id, call_chain, turn_index, extra_metadata=None)`
- `PermissionRequest.for_skill_dispatch(target_skill_id, *, caller_skill_id, call_chain, thread_id, submission_id, entry_skill_id, turn_index, extra_metadata=None)`

其中 `extra_metadata: dict[str, Any] | None` 为业务侧不透明上下文，原样合并进 `metadata`（taifeng 不解析；无业务命名字段，R1）。

mypy strict 模式下漏传必填字段 SHALL 报错。

#### Scenario: 工厂方法漏传必填字段
- **WHEN** 代码写 `PermissionRequest.for_tool_call("foo", {})`（漏 thread_id 等）
- **THEN** mypy strict SHALL 报错（required keyword missing）

#### Scenario: 工厂方法构造结果
- **WHEN** `PermissionRequest.for_skill_dispatch("D", caller_skill_id="C", call_chain=("A","B","C"), thread_id="t1", submission_id="s1", entry_skill_id="A", turn_index=3)`
- **THEN** 返回 PermissionRequest 含 scope='skill_dispatch' / target='D' / 全部 typed 字段填充

### Requirement: PermissionPolicy 新增 prompter_timeout_seconds

`PermissionPolicy` SHALL 新增字段：

- `prompter_timeout_seconds: float = 0`（0 = 不超时，原行为）
- `telemetry: TelemetrySink | None = None`（用于发 `permission_prompt_timeout`）

`prompter_timeout_seconds > 0` 时，系统 SHALL 用 `anyio.fail_after(prompter_timeout_seconds)` 包裹 `prompter.prompt(...)` 调用。超时 SHALL：

1. 取消 prompter 内部 await
2. 发 EventMsg `permission_prompt_timeout`（含 `scope / target / timeout_seconds / call_chain`）
3. 返回 `PermissionDecision.deny(reason="prompter_timeout_{N}s")`
4. **不**重试

`prompter_timeout_seconds == 0` 时，SHALL NOT 包 fail_after（性能不退化）；prompter 自己负责超时。

#### Scenario: prompter 慢响应触发 timeout
- **WHEN** CallbackPrompter 内部 `await anyio.sleep(5)`，policy.prompter_timeout_seconds=1
- **THEN** `check()` SHALL 在 1.5s 内返回 deny；prompter 内部 sleep 被取消

#### Scenario: 业务 prompter 自带 timeout (taifeng timeout=0)
- **WHEN** policy.prompter_timeout_seconds=0，业务 prompter 自带 `wait_for(timeout=10)`
- **THEN** 行为不变（原有兼容路径）；不发 `permission_prompt_timeout` 事件

#### Scenario: 超时后不重试
- **WHEN** 一次 prompter 超时 deny 后，同 turn 再次触发同样 scope/target
- **THEN** 走完整 check 流程（包括可能再次询问 prompter）；前一次 timeout 不缓存 deny 决策

### Requirement: EnginePool 暴露 permission_policy 注入参数

`EnginePool.__init__` 与 `EnginePool.create` SHALL 新增 keyword-only 参数 `permission_policy: PermissionPolicy | None = None`。

- 为 `None` 时（默认）：行为与改造前完全一致，PermissionPolicy 不参与
- 非 None 时：pool 持有该 policy，所有 `get_or_create` 返回的 `AgentEngine` SHALL 透传该 policy 到内部 TurnRunner

`EnginePool` SHALL 同时新增 `request_metadata: dict[str, Any] | None = None` 参数，用于业务侧统一注入不透明上下文，原样合并进 `PermissionRequest.metadata`（无业务命名字段，R1；taifeng 不解析其 keys）。

#### Scenario: pool 构造时不传 permission_policy 不影响既有行为
- **WHEN** 业务侧 `await EnginePool.create(skills_dir=..., model_client=..., storage_dir=...)` 不传 `permission_policy`
- **THEN** pool / engine / turn / tool 链路 SHALL 与改造前完全一致
- **AND** TurnRunner.permission_policy SHALL 为 None

#### Scenario: pool 注入 permission_policy 后透传到 TurnRunner
- **WHEN** 业务侧 `await EnginePool.create(..., permission_policy=my_policy)`
- **AND** 启动一个 turn
- **THEN** TurnRunner 实例的 `permission_policy` 字段 SHALL 等于 `my_policy`
- **AND** 子 skill 派发时子 TurnRunner.permission_policy 也 SHALL 等于 `my_policy`（既有"透传到子 turn"行为不变）

### Requirement: AgentEngine 透传 permission_policy / request_metadata 到 TurnRunner

`AgentEngine.__init__` SHALL 新增 keyword-only 参数 `permission_policy: Any = None` 与 `request_metadata: dict[str, Any] | None = None`，保存为 instance 字段。

`AgentEngine._run_turn_for` 构造 TurnRunner 时 SHALL 把 `self._permission_policy` 与 `self._request_metadata` 作为 kwarg 传给 TurnRunner。

#### Scenario: engine 不传 permission_policy 时 TurnRunner 收到 None
- **WHEN** 直接构造 `AgentEngine(...)` 不传 permission_policy
- **THEN** TurnRunner.permission_policy SHALL 为 None

#### Scenario: engine 注入 permission_policy 后 TurnRunner 收到同对象
- **WHEN** `AgentEngine(..., permission_policy=p)`
- **AND** 该 engine 跑一个 turn
- **THEN** 该 turn 的 TurnRunner.permission_policy SHALL `is` p（同对象引用）

### Requirement: McpPrompter 是 PermissionPrompter 协议的官方 MCP 实现

系统 SHALL 在 `src/taifeng/mcp/prompter.py` 提供 `McpPrompter` 类，实现 `PermissionPrompter` 协议（async `prompt(request) -> PermissionDecision`）。

构造签名：

```python
McpPrompter(
    server: McpStdioServer,
    *,
    timeout_seconds: float = 60.0,
)
```

`prompt(request)` 实现 SHALL：

1. 构造 elicitation/create params：
   - `message: str` —— 人类可读 prompt，含 `request.scope` / `request.target` / call_chain 摘要
   - `requestedSchema: dict` —— JSON Schema 要求 client 返回 `{"approved": bool, "reason": str?}`
2. 调 `server.server_initiated_request("elicitation/create", params, timeout=timeout_seconds)`
3. 解析响应：
   - `action="accept"` + `content.approved=true` → `PermissionDecision.allow(reason=content.reason or "user_approved", remember="once")`
   - `action="accept"` + `content.approved=false` → `PermissionDecision.deny(reason=content.reason or "user_denied")`
   - `action="reject"` → `PermissionDecision.deny(reason="user_rejected")`
   - `action="cancel"` → `PermissionDecision.deny(reason="user_cancelled")`
   - 其他 `action` → `PermissionDecision.deny(reason=f"elicitation_unknown_action:{action}")`
4. `TimeoutError` 捕获 → `PermissionDecision.deny(reason="elicitation_timeout")`
5. 任何其他异常 → `PermissionDecision.deny(reason=f"elicitation_error:{type(e).__name__}")`，并 log exception

#### Scenario: 用户批准
- **WHEN** McpPrompter.prompt(req) 被调用
- **AND** mock server 收到 elicitation/create，回 `{"action":"accept","content":{"approved":true,"reason":"looks fine"}}`
- **THEN** SHALL 返回 `PermissionDecision(granted=True, mode="allow", reason="looks fine", remember_until="once")`

#### Scenario: 用户拒绝
- **WHEN** mock server 回 `{"action":"accept","content":{"approved":false,"reason":"unsafe"}}`
- **THEN** SHALL 返回 `PermissionDecision(granted=False, mode="deny", reason="unsafe")`

#### Scenario: 客户端 action=reject
- **WHEN** mock server 回 `{"action":"reject"}`
- **THEN** SHALL 返回 deny with reason `"user_rejected"`

#### Scenario: 超时
- **WHEN** McpPrompter 配 timeout_seconds=0.1 + mock server 不回包
- **THEN** SHALL 返回 deny with reason `"elicitation_timeout"`

#### Scenario: elicitation request params 包含 message + schema
- **WHEN** McpPrompter.prompt(req) 被调用，req=PermissionRequest(scope="shell_exec", target="ls /etc", call_chain=("A","B"))
- **THEN** server.server_initiated_request 收到的 params dict SHALL 含：
  - `params["message"]` 字符串 含 "shell_exec" 与 "ls /etc"
  - `params["requestedSchema"]["type"] == "object"`
  - `params["requestedSchema"]["properties"]["approved"]["type"] == "boolean"`
  - `params["requestedSchema"]["required"] == ["approved"]`

### Requirement: PermissionRule 支持 args 级匹配

`PermissionRule` SHALL 新增 optional 字段 `args_match: dict[str, str] | None`（默认 `None`）。
当 `args_match` 非 None 时，`matches(request)` SHALL 在 scope + target 命中之外，额外检查 `request.metadata["args"]` 字典：
- 对 `args_match` 的每个 `(key, pattern)`：取 `args.get(key)`，按 pattern 语义匹配
- **AND 语义**：所有 key 都匹配才返回 True；任一不匹配 → 返回 False
- args 缺少 key → 不匹配（保守）
- request.metadata 缺 `"args"` 字段（如 skill_dispatch scope）→ args_match 非空时整条 rule 不命中

pattern 三态语义：
- **字面**：`"openspec --help"` → 严格等于（`str(args[key]) == pattern`）
- **正则**：`"re:^rm\\s+-rf.*"` → `re.search` 匹配
- **glob**：`"glob:openspec *"` → `fnmatch.fnmatch` 匹配（`*` / `?` / `[seq]` 三种通配符）

#### Scenario: args_match 命中
- **WHEN** rule = `PermissionRule(scope="tool_use", target_pattern="shell_exec", args_match={"cmd": "openspec --help"}, mode="allow")`
- **AND** request = `for_tool_call("shell_exec", {"cmd": "openspec --help"}, ...)`
- **THEN** `rule.matches(request) == True`

#### Scenario: glob 通配符
- **WHEN** rule = `PermissionRule(scope="tool_use", target_pattern="shell_exec", args_match={"cmd": "glob:openspec *"}, mode="allow")`
- **AND** request 的 `args.cmd == "openspec instructions proposal --change x"`
- **THEN** `rule.matches(request) == True`
- **AND** 同 rule 对 `args.cmd == "rm -rf /"` 不命中

#### Scenario: 正则前缀
- **WHEN** rule = `PermissionRule(scope="tool_use", target_pattern="shell_exec", args_match={"cmd": "re:^(ls|pwd|whoami)\\b"}, mode="allow")`
- **AND** request 的 `args.cmd == "ls -la"`
- **THEN** 命中（允许只读命令）
- **AND** 同 rule 对 `args.cmd == "ls; rm -rf /"` 不命中（`;` 后续段不在表达式范围）

#### Scenario: args 缺 key → 不命中
- **WHEN** rule args_match 含 `{"cmd": "..."}` 但 request.metadata.args 不含 `"cmd"` 键
- **THEN** rule.matches(request) == False（保守拒绝匹配，让下一条规则或 default_mode 决定）

#### Scenario: 没有 args_match 的规则 — 行为不变
- **WHEN** rule = `PermissionRule(scope=..., target_pattern=..., mode=...)` 不传 args_match
- **THEN** matches 行为完全等价于本 change 之前的版本（向后兼容）

### Requirement: `PermissionRule.parse` —— Claude Code 风格语法糖

`PermissionRule` SHALL 暴露 `@classmethod parse(rule_str: str, *, mode: PermissionMode) -> PermissionRule`，把字符串语法 `<Alias>(<args>)` 解析成 PermissionRule 对象。

内置 alias 映射表（最小可用集）：

| 前缀 | scope | target | args_match key |
| --- | --- | --- | --- |
| `Bash` / `ShellExec` | `tool_use` | `shell_exec` | `cmd` |
| `Skill` | `skill_dispatch` | （括号内为 target_pattern） | — |
| `Script` | `script_exec` | （括号内为 target_pattern） | — |
| `FileRead` | `tool_use` | `file_read` | `path` |
| `FileWrite` | `tool_use` | `file_write` | `path` |
| `ApplyPatch` | `tool_use` | `apply_patch` | （括号内为 target_pattern） |

括号内为空字符串、`*`、`glob:*` → target / args_match 取 `"glob:*"`（全匹配）。

未识别 alias 前缀 → 抛 `ValueError("unknown_permission_syntax: ...")`。

#### Scenario: parse Bash 字面
- **WHEN** `PermissionRule.parse("Bash(openspec --help)", mode="allow")`
- **THEN** 返回 `PermissionRule(scope="tool_use", target_pattern="shell_exec", args_match={"cmd": "openspec --help"}, mode="allow")`

#### Scenario: parse Bash + glob 通配
- **WHEN** `PermissionRule.parse("Bash(openspec *)", mode="allow")`
- **THEN** 返回 rule 的 `args_match["cmd"] == "glob:openspec *"`

#### Scenario: parse Bash + 正则前缀透传
- **WHEN** `PermissionRule.parse("Bash(re:^rm\\s+-rf\\s+\\./data)", mode="allow")`
- **THEN** 返回 rule 的 `args_match["cmd"] == "re:^rm\\s+-rf\\s+\\./data"`

#### Scenario: parse Skill 通配
- **WHEN** `PermissionRule.parse("Skill(read_*)", mode="allow")`
- **THEN** 返回 `PermissionRule(scope="skill_dispatch", target_pattern="glob:read_*", mode="allow", args_match=None)`

#### Scenario: parse 未知前缀
- **WHEN** `PermissionRule.parse("Unknown(x)", mode="allow")`
- **THEN** 抛 `ValueError`，消息含 `"unknown_permission_syntax"` + 原字符串

### Requirement: `PermissionPolicy.from_dict` —— JSON / dict 直接加载

`PermissionPolicy` SHALL 暴露 `@classmethod from_dict(config: dict, *, prompter=None, telemetry=None, prompter_timeout_seconds=0) -> PermissionPolicy`。

支持两种结构（**自动识别**，混用 SHALL 抛 ValueError）：

**Style A — 语法糖（list of strings）**：
```python
{
    "default_mode": "ask",  # 可选，缺省 "ask"
    "allow": ["Bash(openspec --help)", "Bash(openspec *)", "Skill(read_*)"],
    "deny":  ["Bash(rm -rf *)"],
    "ask":   ["Bash(*)"],
}
```
每个字符串走 `PermissionRule.parse(s, mode=<对应 mode>)`；最终 rules 顺序：deny → allow → ask（deny 优先确保拦截）。

**Style B — 明文规则（list of objects）**：
```python
{
    "default_mode": "allow",
    "rules": [
        {"scope": "tool_use", "target": "shell_exec",
         "args_match": {"cmd": "re:^openspec\\s"}, "mode": "allow",
         "reason": "ops_safe_subcommands"},
        ...
    ],
}
```
每个 object 直接构造 `PermissionRule(**obj)`（已知 key 透传）。

#### Scenario: Style A 加载
- **WHEN** `PermissionPolicy.from_dict({"allow": ["Bash(openspec --help)"]})`
- **THEN** 返回的 policy.rules 含 1 条由 `parse("Bash(openspec --help)", mode="allow")` 生成的规则
- **AND** `policy.default_mode == "ask"`（缺省）

#### Scenario: Style B 加载
- **WHEN** `PermissionPolicy.from_dict({"rules": [{"scope": "tool_use", "target": "shell_exec", "mode": "allow"}], "default_mode": "allow"})`
- **THEN** 返回的 policy 含 1 条规则且 default_mode="allow"

#### Scenario: 混用 → 报错
- **WHEN** dict 同时含 `"allow"` 与 `"rules"`
- **THEN** 抛 `ValueError("permission_config_conflict: cannot mix Style A and Style B")`

#### Scenario: deny 规则优先于 allow（同 dict 内）
- **WHEN** `from_dict({"allow": ["Bash(rm *)"], "deny": ["Bash(rm -rf *)"], "default_mode": "ask"})`
- **AND** request 的 `args.cmd == "rm -rf /tmp"`
- **THEN** 命中 deny → `policy.check` 返回 `deny`（不被 allow 覆盖，因为 deny 在 rules 列表前面）

### Requirement: PermissionPolicy 内核不持久化决策

`PermissionPolicy.check(request)` SHALL **不**因 `PermissionDecision.remember_until == "always"` 而修改 `self.rules`。

业务侧若要 session / 持久层级记忆，SHALL 在自己的 prompter 包装层处理（读 `decision.remember_until` 后自己 append rule 或写存储）。

`PermissionDecision.remember_until` 字段保留作信息字段，业务侧可读但 Taifeng 内核不消费。

#### Scenario: remember="always" 不再自动写回
- **WHEN** prompter 返回 `PermissionDecision.allow(remember="always")`
- **THEN** `policy.check` 正常返回该 decision
- **AND** `policy.rules` 长度不变（不自动 append）
- **AND** 下次同样的 request 再次走 prompter（不被自动学习覆盖）

#### Scenario: 业务侧自管 always 记忆
- **WHEN** 业务侧包装 prompter，在收到 `remember="always"` 时显式 `policy.rules.append(new_rule)`
- **THEN** 下次同 request 命中 business 添加的规则，不走 prompter

### Requirement: 可复用审批 grant（permission-grants）

`PermissionPolicy` SHALL 内置一个 `GrantStore`，承载**可复用审批 grant**——把一次性的
`_preapproved_call_ids`（精确 call_id、用一次即弃，resume 专用）推广为
**作用域化、有确定性生命周期、内核消费并打事件**的凭证。grant 的语义严格是
「人本来会在这个 ask 上点的 yes，提前缓存」，与 `remember_until`（仍 userspace 自管、内核不消费）**互补而非替代**。

`PermissionGrant`（frozen dataclass）SHALL 含字段：

- `scope: PermissionScope` + `target_pattern: str`（+ 可选 `args_match: dict[str,str]`）
  —— 匹配**复用 `PermissionRule.matches`**（literal / `re:` / `glob:` 三态），不重写匹配逻辑
- `call_chain_prefix: tuple[str,...] = ()` —— **子树收窄**：`()`=全树（贴合 engine 级单例 policy 全树共享的事实）；非空仅当它是 `request.call_chain` 的前缀时命中（子树可用、父/兄弟子树不可用）
- `thread_id: str = ""` —— `""`=任意 thread；非空仅命中该 thread
- `max_uses: int | None = None` —— **确定性生命周期**：`None`=不限次；否则每次命中递减、到 0 移除。**禁挂钟 TTL**（`src/` 禁 `Date.now`，保 resume 确定性）；挂钟过期留 userspace 经 `revoke_grant` 处理
- `grant_id: str = ""` —— 审计 / 撤销键；空则 `GrantStore.add` 分配确定性计数 id（无随机、resume 可复现）

`PermissionPolicy` SHALL 提供 `issue_grant(grant) -> PermissionGrant`（亦为 resume 重种入口）与 `revoke_grant(grant_id) -> bool`。

**核心安全不变量**：grant 短路 SHALL 排在规则裁决**之后**——`deny`/`allow` 规则先决，grant 只在 `mode == "ask"` 时、prompter 之前介入。故 grant 只省去重复弹窗，**绝不越过 `deny` 规则、绝不提升权限上限**。

grant 命中 / 签发 / 失效 SHALL 经 `PolicyTelemetryCallback` 发
`permission_grant_hit` / `permission_grant_issued` / `permission_grant_expired` 事件（permission 包不依赖 `loop/event`，R1 干净）。

grant 随 engine 级单例 `PermissionPolicy` 天然全树共享（root / call_skill / spawn_skill 子 runner 共用同一 policy），无需贯穿 pool/engine。

#### Scenario: grant 命中绕过 prompter
- **WHEN** `policy.issue_grant(PermissionGrant(scope="custom", target_pattern="x"))` 且默认 `ask`
- **AND** `policy.check(request(scope="custom", target="x"))`
- **THEN** 返回 `allow`（reason 以 `grant:` 开头）且 **prompter 不被调用**

#### Scenario: grant 绝不越过 deny 规则
- **WHEN** policy 含一条 `deny` 规则命中 target，且又 `issue_grant` 同 target
- **THEN** `policy.check` 返回 `deny`（grant 顶不翻 deny；prompter 亦不被调用）

#### Scenario: prompter 签发的 grant 被记账复用
- **WHEN** prompter 返回 `PermissionDecision.allow(grant=PermissionGrant(...))`
- **THEN** 首次 check 走 prompter（问人一次）
- **AND** 下次同模 request 命中该 grant、自动 `allow`、不再问人

#### Scenario: _preapproved_call_ids 优先于 grant
- **WHEN** 同时存在一张 `max_uses=1` 的 grant 与对该 call_id 的 `preapprove`
- **THEN** check 走 `resume_preapproved`（非 grant）
- **AND** grant 未被消耗（下次无预批时仍可命中）

#### Scenario: max_uses 用尽后回落 prompter
- **WHEN** grant `max_uses=1`，连续两次同模 check
- **THEN** 第一次走 grant `allow`、第二次回落 prompter
- **AND** 发 `permission_grant_expired` 事件

#### Scenario: call_chain_prefix 子树收窄
- **WHEN** grant `call_chain_prefix=("root","expert")`
- **THEN** `request.call_chain=("root","expert","leaf")` 命中
- **AND** `("root",)`（父）/ `("root","other")`（兄弟）均不命中

