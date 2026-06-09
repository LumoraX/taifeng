# examples/ —— 按场景分组的示例索引

所有示例都 self-contained，可独立运行。从下面挑一个对应你正在调研的场景。

## 共享辅助

| 文件 | 用途 |
| --- | --- |
| [_provider_bootstrap.py](_provider_bootstrap.py) | 从环境变量 + `.env` 构造 `ModelClient`，支持 openai / anthropic / gemini / deepseek |

## 单文件示例（按主题分组）

### `basic/` —— 入门必读，纯 MockClient 无需 API key

| 文件 | 演示内容 |
| --- | --- |
| [basic/minimal_chat.py](basic/minimal_chat.py) | 单 entry skill + Mock + 控制台输出，最小骨架 |
| [basic/composite_skill.py](basic/composite_skill.py) | `call_skill` 嵌套派发 + 调用栈 + 环检测 |
| [basic/skill_with_script.py](basic/skill_with_script.py) | SKILL.md `scripts:` 字段 + `run_script` 工具 (shell + python) |
| [basic/instructions_basic.py](basic/instructions_basic.py) | 两层 instructions 注入 + 热更 + snapshot |

运行：`PYTHONPATH=src uv run python examples/basic/<file>.py`

### `kernel_knobs/` —— 内核资源旋钮（能力体验，纯 MockClient 无需 API key）

| 文件 | 演示内容 |
| --- | --- |
| [kernel_knobs/demo.py](kernel_knobs/demo.py) | 业务怎么把微内核的 K1 spawn 配额 / K2 token OOM 强制 / K3 memory 换页 / K4 流控经 `EnginePool.create` 接出 + `introspect()` 自省 |

> 这是"能力体验类"独立 demo（不注入 web_ui）；真实 LLM 版见 [real_llm/kernel_knobs.py](real_llm/kernel_knobs.py)。配置项清单见 [docs/configurable-knobs.md](../docs/configurable-knobs.md) §1.0。

运行：`PYTHONPATH=src uv run python examples/kernel_knobs/demo.py`

### `turn_rewind/` —— 自治链一键跑完 + 回退到任意节点重跑（纯 MockClient 无需 API key）

| 文件 | 演示内容 |
| --- | --- |
| [turn_rewind/demo.py](turn_rewind/demo.py) | 把一次 root turn 拆成可寻址回访节点表；`Rewind(node_id, retry_tool)` 重跑自治链里的一次 `call_skill`；`Rewind(node_id, re_reason)` 回退到某圈采样前让 LLM 重新决定 |

> 子 skill 全程 `entry: false`，绕开 entry/call_skill 互斥。契约见 [docs/architecture/capabilities/turn-rewind.md](../docs/architecture/capabilities/turn-rewind.md)，决策见 [ADR 0014](../docs/decisions/0014-turn-rewind.md)。与 [step_pipeline/](step_pipeline/)（业务层确定性编排）互为两种范式。**已接入 web_ui**（demo_id `turn_rewind`，`wants_rewind=True`）；浏览器交互版见 [web_ui/](web_ui/)。

运行：`PYTHONPATH=src uv run python examples/turn_rewind/demo.py`

### `multi_expert_consult/` —— 并发多专家 + 错峰 HITL + 联合会诊聚合（纯 MockClient 无需 API key）

| 文件 | 演示内容 |
| --- | --- |
| [multi_expert_consult/demo.py](multi_expert_consult/demo.py) | detached-spawn 完整闭环：orchestrator 一个 turn 内 `spawn_skill` 并发发起多个专家 + `await_skills` 登记 join-barrier；各专家在独立 child thread 上**错峰 HITL**（cardio 先恢复完成、metabolic 过一会才恢复）；两句柄全终态 → join-barrier 自动起 `joint-consult` 聚合 → 最终会诊报告。打印完整事件时间线 |
| [multi_expert_consult/nested_hitl_demo.py](multi_expert_consult/nested_hitl_demo.py) | **嵌套专科错峰 HITL**（真实 MDT 拓扑）：被 spawn 的专科是 composite 且 `call_skill` 编排**子 skill**，由子 skill `request_user_input` 挂起 → spawn 子 thread 以 `CHILD_SKILL` 嵌套挂起 → `Resume` 走 `resume_spawn_nested` 续跑链（下探 leaf 核销 + 逐层回填 + 重跑根）→ spawn_completed。区别于 `demo.py` 的 tool-only 专科（直接 DATA 挂起） |

> 本 demo 专家 / 聚合器用 `entry: false`（一种设计选择）。注意 **spawn 目标可为 entry skill**（spawn 是独立根，与 call_skill 不同；详见契约 `allow_entry_target`），并非硬性要求非 entry。契约见 [docs/architecture/capabilities/detached-spawn.md](../docs/architecture/capabilities/detached-spawn.md)，决策见 [ADR 0015](../docs/decisions/0015-detached-skill-spawn.md)。与 [concurrent_fanout/](concurrent_fanout/)（批量同步收齐）、[step_pipeline/](step_pipeline/)（业务确定性编排）互为三种并发姿态。**已接入 web_ui**（demo_id `multi_expert_consult`，`streams_detached=True` + `wants_spawn_tools=True`）；浏览器交互版见 [web_ui/](web_ui/)。

运行：`PYTHONPATH=src uv run python examples/multi_expert_consult/demo.py`

### `real_llm/` —— 真实 LLM 验证，需要 API key

| 文件 | 演示内容 |
| --- | --- |
| [real_llm/e2e.py](real_llm/e2e.py) | 完整 turn + read_skill + cache 命中率验证 |
| [real_llm/composite.py](real_llm/composite.py) | LLM 主动触发 `call_skill` 子 skill 派发 |
| [real_llm/with_hooks.py](real_llm/with_hooks.py) | PreToolUse hook 真实拦截 + permission gate |
| [real_llm/kernel_knobs.py](real_llm/kernel_knobs.py) | 内核旋钮 K1–K4 在真实 token 流下成立（K2 OOM 用真实 usage 触顶 + K3 memory 钩子真触发） |
| [real_llm/capability_matrix.py](real_llm/capability_matrix.py) | **能力矩阵** —— 10 个能力场景逐个真实 LLM 跑测，输出成败矩阵 + R3 可观测完整性审计（所有事件 kind 是否都有专用渲染） |
| [real_llm/nested_spawn_hitl.py](real_llm/nested_spawn_hitl.py) | **嵌套 spawn 错峰 HITL 续跑** 真实 LLM 验证：专科 call_skill 子 skill → 子 skill request_user_input 嵌套挂起（CHILD_SKILL）→ Resume → `resume_spawn_nested` 续跑链跑到终态（补 capability_matrix 不覆盖的 spawn+挂起+续跑盲区） |

运行前：`export LLM_BOOTSTRAP_PROVIDER=openai LLM_BOOTSTRAP_API_KEY=sk-...`
运行：`PYTHONPATH=src uv run python examples/real_llm/<file>.py`

### `permission/` —— 权限策略

| 文件 | 演示内容 |
| --- | --- |
| [permission/web_prompter.py](permission/web_prompter.py) | `CallbackPrompter` + `prompter_timeout_seconds` + Web SSE 风格回调骨架 |

### `observability/` —— 钩子、审计、可观测

| 文件 | 演示内容 |
| --- | --- |
| [observability/audit_index_hook.py](observability/audit_index_hook.py) | `IndexHook` 把 thread 生命周期事件投递到本地 JSONL 审计日志 |

### `persistence/` —— 存储后端骨架（业务侧复制即用）

| 文件 | 演示内容 |
| --- | --- |
| [persistence/postgres_thread_directory.py](persistence/postgres_thread_directory.py) | `ThreadDirectory` PostgreSQL 实现骨架（asyncpg） |
| [persistence/redis_thread_directory.py](persistence/redis_thread_directory.py) | `ThreadDirectory` Redis 实现骨架（redis>=5.0） |

## 多文件示例（每个目录一个完整 demo）

| 目录 | 说明 |
| --- | --- |
| [code_review/](code_review/) | programmer ↔ code-review 双 skill 派发 (HITL 演示底座) |
| [numeric_loop/](numeric_loop/) | LLM 自主多轮 `run_script(apply_delta)` 数值调谐 |
| [travel_planner/](travel_planner/) | 三路 fan-out 子 skill（航班/酒店/活动）+ 综合输出 |
| [research_assistant/](research_assistant/) | 三步串行 pipeline：采集 → 提炼 → 写作 |
| [product_review/](product_review/) | fan-out 三个 reviewer + 评分聚合 + HITL |
| [selective_approval/](selective_approval/) | 按 scope/target 模式选择性审批 |
| [subagent_isolation/](subagent_isolation/) | 子 skill 隔离策略（mode-auto / mode-strict） |
| [memory/](memory/) | 业务侧实现 `MemoryStore`（K3 长期记忆 backend）：4 个 swap 钩子 prefetch/writeback/on_pre_evict/on_session_end 端到端触发 + 跨 turn 召回 |
| [hooks_showcase/](hooks_showcase/) | 业务钩子 pre/post_skill_dispatch 按*运行时 args* 动态拦截（钩子 vs 声明式权限规则） |
| [mcp_showcase/](mcp_showcase/) | taifeng 作为 MCP client：spawn 自带的最小 MCP server 子进程 + 注册其工具远程调用（已注册进 web_ui）|
| [mcp_basic/](mcp_basic/) | MCP stdio client 连外部 server，自动注册工具 |
| [mcp_hitl/](mcp_hitl/) | MCP 工具调用走 permission gate |
| [suspend_resume/](suspend_resume/) | 表单采集型 HITL 挂起 → 释放实例 → 跨实例重建 → Resume 续跑（R5 头条故事）|
| [web_ui/](web_ui/) | FastAPI + SSE 浏览器实时看 agent 数据流，多 demo 切换 + 权限策略可视化 + 会话级可观测指标聚合面板 + 历史会话续接（R5 resume）；**含两个 detached 交互 demo**：`multi_expert_consult`（并发多专家 + 错峰 HITL + 联合会诊）和 `turn_rewind`（节点回访 + re_reason / retry_tool 重跑）。无 key 自动化 smoke：`PYTHONPATH=src uv run python examples/web_ui/smoke_detached.py` |

### 编排 / 并发范式 + 懒加载（skill-as-context）

| 目录 | 演示内容 | 需要 key |
| --- | --- | --- |
| [read_skill_lazy/](read_skill_lazy/) | `read_skill` 懒加载：子 skill 列表只给 id+description，LLM 按需拉 body（skill-as-context 范式核心，cache 友好） | 否（Mock） |
| [orchestration/](orchestration/) | 声明式编排三原语 parallel / serial / when（执行器不采样 LLM，确定性跑） | 否（Mock） |
| [concurrent_fanout/](concurrent_fanout/) | LLM 在一条消息里 fan-out 多个 `call_skill` 批量同步并发收齐 | 否（Mock） |
| [step_pipeline/](step_pipeline/) | 业务层确定性步级编排（与自治链互为两种范式，见目录内 README） | 否（Mock） |
| [dual_track/](dual_track/) | 同一批核心步骤 skill 既走自治链、又走业务编排步级 retry（wrapper 双轨，见目录内 README） | **是** |

> **三种并发姿态对照**：`concurrent_fanout`（批量同步收齐）↔ `multi_expert_consult`（detached 异步 + join-barrier）↔ `step_pipeline`（业务确定性编排）。

### 上下文压缩

| 文件 | 演示内容 | 需要 key |
| --- | --- | --- |
| [compression_showcase/demo.py](compression_showcase/demo.py) | 本地 budget 到顶**主动压缩**（极小 1024 window + sliding 兜底，phase=pre_turn），不依赖 provider 报错 | 否（Mock） |
| [compression_showcase/overflow_demo.py](compression_showcase/overflow_demo.py) | provider 判超长 → **overflow 有界自愈**（强制压缩一次 + 重采样一次，phase=overflow，发 `provider_retry`） | 否（Mock） |

> 真实 LLM handoff 摘要压缩见 [real_llm/capability_matrix.py](real_llm/capability_matrix.py) 的 compression 场景。

### skill 包（无独立运行脚本，被其他 demo 复用）

`form_hitl/skills` —— 纯提示型 HITL 表单 skill 包，被 web_ui 复用，**只含 skills/**，无 demo 入口脚本。
