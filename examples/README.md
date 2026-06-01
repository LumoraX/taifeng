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

### `real_llm/` —— 真实 LLM 验证，需要 API key

| 文件 | 演示内容 |
| --- | --- |
| [real_llm/e2e.py](real_llm/e2e.py) | 完整 turn + read_skill + cache 命中率验证 |
| [real_llm/composite.py](real_llm/composite.py) | LLM 主动触发 `call_skill` 子 skill 派发 |
| [real_llm/with_hooks.py](real_llm/with_hooks.py) | PreToolUse hook 真实拦截 + permission gate |
| [real_llm/kernel_knobs.py](real_llm/kernel_knobs.py) | 内核旋钮 K1–K4 在真实 token 流下成立（K2 OOM 用真实 usage 触顶 + K3 memory 钩子真触发） |
| [real_llm/capability_matrix.py](real_llm/capability_matrix.py) | **能力矩阵** —— 10 个能力场景逐个真实 LLM 跑测，输出成败矩阵 + R3 可观测完整性审计（所有事件 kind 是否都有专用渲染） |

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
| [web_ui/](web_ui/) | FastAPI + SSE 浏览器实时看 agent 数据流，多 demo 切换 + 权限策略可视化 + 会话级可观测指标聚合面板 + 历史会话续接（R5 resume）|
