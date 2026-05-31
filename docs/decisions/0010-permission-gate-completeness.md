# ADR 0010: 闭合 permission gate 体系（capability gate 完整闭环）

- 状态：Accepted（部分被 2026-05-29 R1 收口修订）
- 日期：2026-05-24
- Related: spec `docs/architecture/capabilities/permission-gate.md`

> **🔧 2026-05-29 修订（R1 收口）**：本 ADR 当年把 `tenant_hint: str | None` 作为
> "R1 合规的透传字段"。复查后认定：业务命名字段（即便是不透明透传）仍违反 R1
> "禁止领域名词（任何语言）"。已移除 `tenant_hint`，业务侧领域上下文（租户 / 用户 /
> audience 等）统一走开放的 `PermissionRequest.metadata` dict + `EnginePool/AgentEngine/
> TurnRunner` 的 `request_metadata` 透传参数 + 工厂方法 `extra_metadata`。本文下文凡提
> `tenant_hint` 之处，语义等价替换为"`metadata` 中业务自定义的不透明键"。

## 背景

taifeng 的 permission gate 体系自 P0/P1（commit `903ac77`）落地以来已具备：
`PermissionPolicy + PermissionRule + PermissionPrompter` 三件套；
`PermissionScope` 包含 8 类（含 `skill_dispatch`、`script_exec`、`shell_exec`）；
`ToolCallRuntime` 在执行工具前调用 `policy.check(...)`；
`HookRunner` 已串联 `pre_tool_use → permission → tool → post_tool_use` 主链路。

**审计发现 4 个具体缺口**（详见 design.md §Context）：

| # | 缺口 | 影响 |
|---|---|---|
| **G1** | `call_skill` handler 只过 DispatchPolicy（白名单/深度/环），**不调** `PermissionPolicy.check` | 业务无法运行时热控"租户级 child skill 黑名单" |
| **G2** | `PermissionRequest.metadata: dict[str, Any]` 是 open-ended 约定 | 内部 handler 塞 key 不统一；业务 prompter 写防御代码 |
| **G3** | `PermissionPolicy.check` 直接 `await prompter.prompt(...)`，无内置 timeout | 业务 prompter 忘加 timeout → engine 永久阻塞 |
| **G4** | `HookKind` 不含 `pre/post_skill_dispatch` | 业务想审计 skill dispatch 只能 `if tool_name == 'call_skill'`，不优雅 |

用户在审计反馈中明确：
> taifeng 是 capability gate（"能不能干"），不是 identity service（"谁干的"）。
> 4 个缺口属于"还没干完的事"，不是"做错了的事"——加号设计闭合即可，不动现有 API。

## 决策

### G1：call_skill 走 PermissionPolicy + 新 Hook lifecycle（5 阶段固定顺序）

`call_skill` handler 重写为：

```
1. DispatchPolicy.check（结构性 —— 环检测 / 深度上限 / 白名单）
   ↓ 失败立即返回（fail-fast，不进入后续）
2. pre_skill_dispatch hook 链
   ↓ 任一 deny → 返回 error + emit skill_dispatch_hook_denied
3. PermissionPolicy.check（业务策略 —— 租户级 / 配额）
   ↓ deny → 返回 error + emit skill_dispatch_permission_denied
4. dispatcher.run_sub_skill（push call_stack + 子 TurnRunner）
5. post_skill_dispatch hook 链（run_audit_only —— 仅审计，不影响 ToolResult）
6. 返回 ToolResult
```

**顺序锁定理由**：
- DispatchPolicy 是**结构性**保证（环检测、深度上限）；结构错的请求连业务策略都不该看到
- 与 PreToolUse → Permission → Tool → PostToolUse 既有模式对称（业务侧心智一致）
- 子 turn 失败时 post hook 也触发（`PostSkillDispatchHook.success=False`），便于审计完整闭环

`ctx.extras` 新加 `permission_policy: PermissionPolicy | None`；为 None 时跳过 step 3（向后兼容老业务）。

### G2：PermissionRequest 走"keyword-only 新字段 + 工厂方法"双层 schema

- 新增 6 个 typed 字段（全部 keyword-only + 默认值，向后兼容旧构造）：
  `thread_id` / `submission_id` / `entry_skill_id` / `call_chain` / `turn_index` / `tenant_hint`
- `metadata: dict` 保留作为业务自定义扩展点（taifeng 内部不再向其塞标准字段）
- 新增 3 个工厂方法 `for_tool_call / for_script_exec / for_skill_dispatch`；
  其中 `thread_id / submission_id / entry_skill_id / turn_index` 被声明为 required keyword
  —— mypy strict 漏传报错；运行时漏传 TypeError
- taifeng 内部所有 handler 切到工厂方法构造，强制走 schema

**call_chain 长度上限 32**：超过截断到末尾 32 个（保留最近调用栈）。理由：
默认 `max_call_depth=6`，正常永不触发；上限只是边界保护防止 telemetry / metadata 体积爆炸。

### G3：PermissionPolicy.prompter_timeout_seconds（默认 0 = 不超时）

- 新字段 `prompter_timeout_seconds: float = 0`（0 = 不包 `anyio.fail_after`，性能不退化）
- `>0` 时用 `anyio.fail_after(timeout)` 包裹 `prompter.prompt(...)`；
  超时取消 prompter 内 await → 发 `permission_prompt_timeout` 事件 → 返回 `deny(reason="prompter_timeout_{N}s")`
- **不重试** —— 业务 prompter 自决重试策略
- 新增 `telemetry: PolicyTelemetryCallback | None`；超时事件通过此回调发出

**默认 0 不是 30s 的理由**：
- 兼容性 > 默认值安全性 —— 既有业务 prompter 可能依赖更长等待（人工审批耗时数小时）
- 升级 taifeng 不会让现有业务突然出现 deny
- 但文档强调"生产建议显式设值"（典型 60s / 300s 量级）

### G4：新增 pre_skill_dispatch / post_skill_dispatch hook lifecycle

- `HookKind` literal 增加 `pre_skill_dispatch / post_skill_dispatch`
- 新增数据类：
  - `PreSkillDispatchHook(target_skill_id, args, caller_skill_id, call_chain, depth)`
  - `PostSkillDispatchHook(target_skill_id, caller_skill_id, success, duration_ms, sub_thread_id, output_preview)`
- `output_preview` 最多 `SKILL_OUTPUT_PREVIEW_LIMIT = 1024` 字节截断（防止 hook 数据爆炸）
- `HookRunner.run_audit_only(kind, hook, ctx) -> None`：吞掉 deny / 异常，仅写日志。
  `post_skill_dispatch` **必须**用 `run_audit_only`（spec 强制：post hook 不能改 ToolResult）

## 备选方案（被拒）

### 引入 RBAC / 角色 / 用户模型？

❌ 拒绝。taifeng = capability gate（"能不能干"），不是 identity service（"谁干的"）。
RBAC 涉及 user / role / scope 三元组持久化，会引入 user store、role hierarchy、
audit log 等业务级抽象，违反 R1 业务零侵入。

业务侧通过 `tenant_hint` 字段 + 自定义 `PermissionRule` pattern 实现复杂匹配
（例如基于 `request.tenant_hint` 做 multi-tenant 过滤）。

### 完全替换 metadata=dict 为 typed Pydantic model？

❌ 拒绝。破坏向后兼容 —— 老业务 prompter 代码全部要改 `req.metadata.get(...)` → `req.x`。
当前方案"加号设计"：typed 字段直接读、metadata 保留为开放扩展点。

### 默认 prompter_timeout_seconds = 30s？

❌ 拒绝。破坏向后兼容。既有业务 prompter 可能依赖更长等待（人工审批耗时数小时）。
默认 0 = 显式 opt-in，业务自觉。文档强调生产建议设值。

### 复用 pre_tool_use hook + `if tool_name == 'call_skill'`？

❌ 拒绝。业务侧每个 hook 都要写 dispatcher 判断；call_skill 的语义
（`target_skill_id / call_chain / depth`）与普通 tool（`args: dict`）有结构差异，
硬塞进 `PreToolUseHook` 类型不优雅。

### post_skill_dispatch hook 允许 deny / 改 ToolResult？

❌ 拒绝。子 turn 已经执行完成（IO 副作用已发生），让 hook 撤销不仅无效，
还会让 LLM 看到不一致的状态。post hook 用 `run_audit_only` 明确"仅审计"语义。

业务想拦截，**必须**用 `pre_skill_dispatch` + `PermissionPolicy`。

## 影响

### 公共 API 变更（兼容性）

| 变更 | 兼容性 |
|---|---|
| `PermissionRequest` 新增 6 个 keyword-only 字段（默认值） | 旧 `PermissionRequest(scope=..., target=...)` 仍可用 |
| `PermissionPolicy.__init__` 新增 2 个字段（默认 0 / None） | 旧 `PermissionPolicy(...)` 仍可用；行为不变 |
| `HookKind` literal 增加 2 个值 | additive；旧 hook 注册不变 |
| 新增 EventMsg variants × 3 | additive；旧订阅者收到新事件可忽略 kind |
| 新增 `PreSkillDispatchHook` / `PostSkillDispatchHook` 数据类 | 不影响旧代码 |
| 新增 `HookRunner.run_audit_only` 方法 | 不影响旧调用 |

**破坏性边界**：仅当业务侧用 `dataclasses.fields(PermissionRequest)` 反射或
全字段序列化时受影响。生产用法（构造 + 读字段）100% 兼容。

### 红线（R1–R5）

| 红线 | 影响 | 落实 |
|---|---|---|
| **R1 业务零侵入** | 强化 —— 不引入 RBAC；业务策略走通用 `PermissionRule` pattern | 业务侧通过 `tenant_hint` + 自定义 prompter 表达 |
| **R2 Cache 友好** | 无影响 —— permission gate 不动 history / cache anchor | — |
| **R3 可观测** | 强化 —— 新增 3 个 EventMsg + 6 个 hook lifecycle 完整覆盖 dispatch | TelemetrySink + ConsoleSink 渲染 |
| **R4 可取消** | 强化 —— prompter timeout 防止 prompter 无限 await 阻塞父 turn cancel 传播 | `anyio.fail_after` |
| **R5 可 resume** | 无影响 —— PermissionPolicy.rules 内存态；业务侧自己持久化（如需要） | — |

### 测试

- 单测新增 18 个：
  - `tests/test_permission.py` +12（typed 字段、工厂方法、timeout 路径、telemetry）
  - `tests/test_call_skill_permission.py` +13（5 阶段顺序、permission/hook deny、ctx 透传、output 截断）
  - `tests/test_hooks.py` +5（新 hook kind 注册、frozen、run_audit_only 容错）
- 现有 63 测试不退化（全绿）；新增总 ≥ 30 用例

### 文档

- `docs/architecture/agent-loop.md` —— lifecycle 图加 `pre/post_skill_dispatch` 位置
- `docs/configurable-knobs.md` §6 —— 加 `prompter_timeout_seconds` 完整说明
- `docs/usage.md` —— 新增 "Web prompter 实现" 小节（典型业务侧用法）
- `examples/permission/web_prompter.py` —— 3 个端到端案例

## Open Questions

### Q1. PermissionDecision 的 "记忆 always" 是否需要跨进程持久化？

**当前立场**：不做。业务侧应该在启动时显式加载 initial rules（从 DB / 配置中心），
而不是依赖 taifeng 内存态。本 change 不引入持久化职责。

### Q2. PreSkillDispatchHook 能否改 args？

**当前立场**：第一版不开放。skill 派发的 args 是结构化（target_skill_id 等），
改了会破坏 DispatchPolicy 校验前提。如业务确实需要，下个 change 再开。

### Q3. PermissionRule 是否需要按 call_chain 匹配？

**当前立场**：不做。业务侧可通过 `tenant_hint + 自定义 rule subclass` 实现复杂匹配。
本 change 保持 `PermissionRule` 简单（只匹配 `request.target`）。

## Archive 前自查（T8）

- [x] **R1 业务零侵入**：`grep -rn "role\|rbac\|user_model\|identity" src/taifeng/permission/` 无命中；`tenant_hint` 仅作为 `str | None` 透传字段（taifeng 不解析）
- [x] **R3 可观测**：3 个新 EventMsg 实现 + ConsoleSink 渲染 + 文档：
      `permission_prompt_timeout` / `skill_dispatch_hook_denied` / `skill_dispatch_permission_denied`
- [x] **R4 可取消**：`prompter_timeout` 用 `anyio.fail_after` 包裹；测试覆盖 timeout=0（不退化）/ timeout>0（取消生效）/ 超时不缓存 deny 三条路径
- [x] **G1 验证**：`tests/test_call_skill_permission.py` 含 PermissionPolicy + Hook 串行执行用例（`test_hook_order_pre_before_permission`）；DispatchPolicy 失败时不跑后续（`test_dispatch_policy_failure_does_not_call_hook_or_permission`）
- [x] **G2 验证**：typed 字段在 prompter 测试中被 assert（`test_typed_request_fields_populated` 验证 call_chain / entry_skill_id / tenant_hint 全部传到 prompter）；工厂方法漏字段 → TypeError（`test_factory_methods_force_context`）
- [x] **G3 验证**：timeout=0 与 timeout>0 两种路径都有用例（`test_prompter_no_timeout_when_zero` / `test_prompter_timeout_returns_deny` / `test_prompter_timeout_emits_event`）
- [x] **G4 验证**：pre/post_skill_dispatch hook 都有触发用例 + post hook 在 sub_turn 失败时也触发（`test_post_hook_triggered_on_sub_turn_failure`）+ post hook 仅审计语义（`test_post_skill_dispatch_hook_audit_only`）
- [x] **commit 顺序**：T1+T2 / T3 / T4+T5 / T6 / T7 / T8 共 6 个 commit（T1+T2 / T4+T5 合并因紧耦合 —— typed 字段升级与 prompter timeout 在同一文件改动；call_skill 5 阶段重写依赖新 EventMsg variants）
- [x] **总测试数**：63 baseline + 30 新 = 93 全绿（spec 要求 ≥ 81 / 任务说明 ≥ 84）
- [x] **契约校验**：permission-gate 能力契约严格校验通过
