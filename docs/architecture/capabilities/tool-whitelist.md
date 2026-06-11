# Capability: tool-whitelist（工具白名单一致性）

## Purpose

保证工具白名单在「声明 → 可见 → 可执行」三环节对账一致：skill 声明什么（含 scripts 自动派生），LLM 就能看见什么；LLM 看不见的，引擎绝不执行。修复两类历史缺口：① scripts 声明后 `run_script` 不进请求 tools（脚本能力静默不可用，「atomic + scripts」结构性死路）；② dispatch 只查 registry——LLM 幻觉调用未声明工具仍被真实执行。

## 数据契约

| 结构 | 模块 | 要点 |
| --- | --- | --- |
| `SkillDefinition.visible_tool_names()` | `skill/definition.py` | 可见集**单一真相**：`tool_names ∪ {read_skill, call_skill} ∪（scripts 非空 → {run_script}）`；atomic / composite 一致；消费点不得内联重复集合逻辑 |
| `dispatch_batch(..., visible_tools=)` | `loop/tool_batch.py` | 必填参数：本轮**实际注入请求**的工具名集（registry 过滤后，与请求严格同源） |
| `ToolResult.error(reason="not_offered")` | `tool/spec.py`（复用） | 拒绝执行的机读原因；输出文本 `tool_not_offered: <name>` |

## 行为契约（要点）

1. **声明即可见**：请求 tools = `visible_tool_names()` ∩ registry 已注册（未注册静默不可见——现状保留，同源化后只剩这一处过滤点）。
2. **可见才可执行**：`tool_batch` 在 PreToolUse hook **之前**校验 `req.name ∈ visible_tools`；不在集合 → is_error 的 `function_call_output` 核销 call_id（LLM 可见错误自行恢复，turn 不中断），不消耗 hook / 权限 / 锁资源；`ToolCallCompleted(is_error=True)` 照常 emit（R3）。
3. **传入基准按调用点**：turn 主路径传请求名集（严格同源）；retry_tool 重跑传声明层 `visible_tool_names()`（热重载移除声明则如实拒）；声明式编排 turn 同源传入（只合成 call_skill，内核发起非幻觉面）。
4. **豁免面**：engine 的 resume 重放（原始派发已校验且人已批准）与业务直发工具 Op（非 LLM 发起）不经 batch 层，不做校验。
5. **composite 空壳校验**：`child_skills / tool_names / scripts` 至少其一（scripts-only composite 有 agency，合法）；atomic 仍禁声明 tool_names。

## 测试接入

- 三层专测：`tests/loop/test_tool_whitelist.py`（声明 / 可见（sim ledger 断言 request.tools）/ 可执行（not_offered 拒绝 + hook 零触达））。
- 真实复证：travel_planner 三 finder 即「atomic + scripts」活样例（capability_matrix `travel_planner` 场景）。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| R1 | 纯内核机制，零业务概念 |
| R2 | 可见集确定性（排序进请求）；scripts 并入是一次性结构变化，由 `tool_spec_changed` 指纹归因可观测 |
| R3 | 拒绝路径照常 `ToolCallCompleted`（is_error），无静默吞没 |
| R4 | 不涉长时操作 |
| R5 | resume 重放豁免，挂起/恢复语义不变 |

## 能力边界（如实记录）

- 「声明但 registry 未注册」仍是静默不可见（如 scripts 声明但引擎未配 script_executors 时 run_script 可见但执行报错）——收紧另立 change。
- 校验只覆盖 LLM 发起的 batch 路径；业务直发 Op 的安全由业务侧自担。
