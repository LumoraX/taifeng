<div align="center">

# 泰逢 · Taifeng

**让 LLM 自主调度文档化 skill 的 Python agent 微内核**

[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-622%20passed-brightgreen)](#状态)
[![Status](https://img.shields.io/badge/status-pre--alpha-orange)](#状态)
[![Style](https://img.shields.io/badge/lint-ruff%20%2B%20mypy-purple)](https://github.com/astral-sh/ruff)

[中文](README.md) · [English](README_EN.md)

</div>

---

> 《山海经·中山经》：
> "吉神泰逢司之，其状如人而虎尾，是好居于萯山之阳，出入有光，**泰逢神动天地气也**。"

**泰逢**「动天地气」，恰对应 LLM Agent 核心调度域中那些**看不见的流**：token、event、cache、cancellation。

---

## 是什么

Taifeng 是一个**与业务完全解耦**的 Python LLM Agent **微内核 / OS 调度器**，对标 [codex](https://github.com/openai/codex)（Rust）/ Claude Code（TS）/ [claw-code](https://github.com/ultraworkers/claw-code)（Rust）的 CLI agent 范式，给 Python 服务端项目提供可嵌入的 agent 引擎。

**它不是**：
- ❌ 不是 LangGraph / AutoGen / Letta 的竞品（不同范式：图 / Actor / 记忆）
- ❌ 不是织造工具，不是业务框架
- ❌ 不绑定任何业务概念（无 tenant / 无领域术语 / 无 LLM provider lock-in）

**它是**：
- ✅ **Skill 是 markdown**（不是 function tool）—— LLM 自主读取 SKILL.md，按需展开/派发
- ✅ **LLM 是调度器**（不是被调度对象）—— Engine 负责并发 / 取消 / cache / 持久化，LLM 决策
- ✅ **Cache-aware compaction** —— mid-turn 只改 tail 保 cached prefix；pre-turn 才允许动 head
- ✅ **Submission / EventMsg Actor 双总线** —— 父子级联 cancel，跨层 submission_id 一致
- ✅ **多 provider 抽象** —— OpenAI / Anthropic / Gemini / 任何 OpenAI-compat 端点（via LiteLLM 或原生 httpx）

## 功能一览

- 🧩 **Skill = markdown** —— SKILL.md 自描述，LLM 按需 `read_skill` 展开 / `call_skill` 派发；atomic + composite 递归组合，带深度 / 环检测
- 🔀 **声明式编排** —— parallel / serial / when 条件分支，确定性驱动多技能协作
- 🗜️ **Cache-aware 压缩** —— mid-turn 只动 tail 保 prompt cache anchor；handoff（LLM 接力）+ sliding window 两策略
- 🔌 **多 provider** —— OpenAI / Anthropic / Gemini / DeepSeek 原生直连 + LiteLLM 兜底，统一 `ResponseEvent` 流，prompt cache 命中精准上报
- 🛰️ **MCP 双向** —— 既作客户端连外部 MCP server 自动注册工具，也能反向作为 MCP server 暴露 skill
- 🔐 **HITL 权限 + Hooks** —— Claude Code 风格 `Bash(...)` 规则、per-builtin 审批、PreToolUse / PreCompact 等 8 类 hook
- 💾 **持久化 & resume** —— JSONL 追加写主存 + SQLite 旁路索引，按 `thread_id` 崩溃续接
- ⚡ **Actor 双总线 + 可取消** —— Submission / EventMsg 消息总线，父子级联 `CancellationToken`
- 📊 **可观测** —— Console / JSONL / OTel(OTLP) 三 sink，关键路径全打点

## 快速上手

```bash
# 1. 安装（必须用 uv，不用 pip）
uv venv && uv pip install -e ".[dev,litellm]"

# 2. 跑全量测试（PYTHONPATH=src 是 src-layout 必需）
PYTHONPATH=src uv run pytest tests/

# 3. 端到端示例（MockClient，无需 API key）
PYTHONPATH=src uv run python examples/basic/minimal_chat.py
PYTHONPATH=src uv run python examples/basic/composite_skill.py

# 4. 真实 LLM（需要 OPENAI_API_KEY 等环境变量）
PYTHONPATH=src uv run python examples/real_llm/e2e.py
```

最小骨架（19 行业务代码 + 1 个 SKILL.md）：

```python
import taifeng

# 1) 在 ./skills/hello/SKILL.md 写一段 markdown 自描述
# 2) 业务侧装配 Engine：
pool = await taifeng.EnginePool.create(
    skills_dir="./skills",
    storage_dir="./threads",   # 旧参数名 threads_dir 仍兼容
    model_client=taifeng.LiteLLMClient(model="gpt-4o-mini"),
    compressors=[taifeng.HandoffCompactionStrategy()],
)
engine = await pool.get_or_create(session_id="s1", entry_skill_id="hello")

sub_id = await engine.submit(taifeng.UserMessage(text="你好"))
async for ev in engine.subscribe(sub_id):
    if ev.msg.kind == "assistant_text":
        print(ev.msg.data["delta"], end="", flush=True)
    elif ev.msg.kind in ("turn_completed", "turn_failed"):
        break

await pool.close()
```

> **术语：session / thread / conversation 分属三层，不是同义词**
> - **`session_id`** —— 业务侧自定义的逻辑会话键，`EnginePool` 用它**缓存活跃 Engine 实例**（进程内路由，**不落盘**）。
> - **`thread_id`** —— **持久化 / resume 的最小单元**：每次 `create_thread` 返回一个 thread_id，transcript JSONL 按它分文件，`resume_thread_id` 按它续接。运行时真正的「一段对话」标识符就是 thread_id。
> - **`conversation/`** —— 只是**持久化子系统的模块名**（"对话持久化"），**不是运行时标识符**（代码里没有 `conversation_id`）。一段"对话（conversation）"在物理上就等于一个 thread。
>
> 即：日常代码用 **`thread_id`** 作标识，"conversation" 仅指那个持久化模块。`threads_dir` 是旧参数名，等价于（推荐的）`storage_dir`。

更多示例 → [examples/](examples/)（19 个，覆盖 MCP/HITL/subagent/震荡回归/真实 LLM）。

## 核心 Capability

| 能力 | 模块 | spec |
|---|---|---|
| **统一 Skill 模型** —— atomic + composite + 静态/运行时环检测 | `skill/` | [skill-dispatch](docs/architecture/capabilities/skill-dispatch.md) |
| **Tool 系统** —— RwLock 并行 / 独占调度（`parallel_safe` 字段） | `tool/` | — |
| **内置工具集（10 个）** —— `read_skill` / `call_skill` / `file_read` / `file_write` / `shell_exec` / `apply_patch` / `run_in_background` / `wait_for_task` / `run_script` / **`http_request`** | `tool/builtins/` | [tool-builtins-extended](docs/architecture/capabilities/tool-builtins-extended.md) |
| **Hook lifecycle** —— PreToolUse / PostToolUse / PreCompact / PreTurn / PreSkillDispatch / PostSkillDispatch | `hooks/` | [hooks](docs/architecture/capabilities/hooks.md) |
| **PermissionPolicy + HITL** —— Claude Code 风格 `Bash(...)` / `Network(...)` / `Skill(read_*)` 规则语法 | `permission/` | [permission-gate](docs/architecture/capabilities/permission-gate.md) |
| **MCP stdio client + server mode** —— 外部 MCP 自动注册 / 反向作为 server 暴露 skill | `mcp/` | [mcp-server](docs/architecture/capabilities/mcp-server.md) |
| **Cache-aware 压缩** —— Handoff（codex 范式）+ SlidingWindow + cache_stats | `context/strategies/` | — |
| **LLM 强类型输出** —— `ResponseFormatSpec` + `structured_output` 事件 + 3 provider 统一翻译 | `llm/` | [llm-structured-output](docs/architecture/capabilities/llm-structured-output.md) |
| **Subagent 隔离** —— inherit / auto_deny / auto_allow 三种 PermissionPolicy 包装模式 | `skill/dispatch.py` | — |
| **JSONL transcript + Resume** —— 追加写主存 + SQLite 旁路索引 + thread_id resume | `conversation/` | [jsonl-transcript](docs/architecture/capabilities/jsonl-transcript.md) |
| **Instructions 注入** —— CLAUDE.md / system_prompt / project_instructions 三层 resolver | `loop/prompt.py` | [instructions-injection](docs/architecture/capabilities/instructions-injection.md) |
| **Telemetry** —— ConsoleSink / JsonlSink / **OtelTelemetrySink**（OTLP exporter，业务侧按需开启） | `telemetry/` | [telemetry-otel](docs/architecture/capabilities/telemetry-otel.md) |
| **Script 执行（M4）** —— SKILL.md 内 `scripts:` 派发 Bash/Python，含启发式 deny list | `skill/scripts/` | [script-execution](docs/architecture/capabilities/script-execution.md) |

## 五条红线（R1–R5）

任何变更必须通过的硬约束（详见 [CLAUDE.md](CLAUDE.md)）：

| # | 红线 | 落实方式 |
| --- | --- | --- |
| **R1 业务零侵入** | `src/` 内禁止业务概念：`tenant_id` / `audience` / 领域名词（无论中英文） | 业务侧通过 `AgentPolicy` 钩子注入策略 |
| **R2 Cache 友好** | 压缩动作必须返回 `CompressionResult { cache_invalidated, anchor_preserved_until }` | mid-turn 只改 tail；pre-turn 才允许动 head |
| **R3 可观测** | 关键路径必须打 `EventMsg`（`turn_started` / `tool_dispatched` / `compaction_attempted` / `cache_break_detected` / `provider_retry`） | 通过 `TelemetrySink` 协议，不绑后端 |
| **R4 可取消** | 长时操作必须接收 `CancellationToken`；子 agent 通过 `cancel.child()` 派生 | 不允许阻塞主 actor |
| **R5 可 resume** | 默认 store 是 JSONL 追加写；业务侧落 DB 自行实现 `MessageStore` 协议 | `MessageStore` 在 `conversation/store.py` |

## 架构速记

```
src/taifeng/
├── skill/        # §1.1 SkillDefinition (atomic/composite) / loader / registry / dispatch / 环检测
├── tool/         # §1.2 ToolSpec (parallel_safe) / Runtime（RwLock 并行调度）/ 10 个 builtins
├── conversation/ # §1.3 ResponseItem / MessageStore 协议 / JsonlMessageStore + SQLite 旁路
├── context/      # §1.4 ContextBudget / CompressionStrategy / Handoff + Sliding / cache_stats
├── llm/          # §1.5 ModelClient 协议 / ResponseEvent / retry / providers (litellm/openai/mock)
├── loop/         # §1.2 Submission/Op + EventMsg + Engine（主 actor）+ TurnRunner + Pool + Cancellation
├── hooks/        # PreToolUse / PostToolUse / PreCompact / PreTurn（claw-code 范式）
├── permission/   # HITL 审批：PermissionPolicy + Rule（args_match）+ Prompter（CLI / Callback）
├── mcp/          # MCP stdio client + server mode
└── telemetry/    # ConsoleSink + JsonlSink + OtelTelemetrySink（业务侧自接其他后端）
```

按 [ADR 0006「统一 Skill 模型」](docs/decisions/0006-unified-skill-model.md) —— 无独立 `agent/` 包；skill-to-skill 派发归 `skill/dispatch.py`，composite skill 替代 agent 概念。

一次 turn 数据流速记：

```
Submission(UserMessage) → AgentEngine 入队 → TurnRunner.run_turn
  ├─ pre-sampling 压缩检查（动 head 允许）
  ├─ build_prompt（entry_skill body + child skills 列表[只 id+description, 不含 body]）
  ├─ ModelClientSession.stream → ResponseEvent 流
  │    ├─ TextDelta → EventMsg.AssistantText
  │    ├─ ToolCallDone(read_skill)  → 取子 skill body 回流
  │    ├─ ToolCallDone(call_skill)  → DispatchPolicy.check（深度/环/白名单）→ 派子 TurnRunner
  │    ├─ ToolCallDone(其他)        → ToolCallRuntime.dispatch（parallel_safe ? 读锁 : 写锁）
  │    └─ Completed → break
  ├─ mid-turn 压缩检查（只动 tail，保 cache anchor）
  └─ MessageStore.append → JSONL flush
→ AgentEngine emit EventMsg.TurnComplete
```

详见 [docs/architecture/overview.md](docs/architecture/overview.md)。

## 状态

🟢 **M1–M4 + hermes capability gap 全部闭环**（2026-05-28）。

- **622 测试** 全绿（pytest）
- **14k LOC** src，15 个能力契约（[`docs/architecture/capabilities/`](docs/architecture/capabilities/README.md)）
- 跨 codex / Claude Code / claw-code / hermes 的 capability gap 已对齐

最近闭环（按时间倒序）：

| capability | change |
|---|---|
| LLM 强类型输出（`structured_output`） | `2026-05-27-llm-structured-output` |
| Composite skill 三层 E2E（depth/cycle/stack_path 断言） | [tests/skill/test_composite_e2e.py](tests/skill/test_composite_e2e.py) |
| `http_request` builtin（PermissionScope=network 首个使用方） | `2026-05-27-http-request-builtin` |
| `call_skill` LLM 自陈 reason 透传到 HITL / EventMsg | `2026-05-27-call-skill-reason-field` |
| PermissionRule args_match + Claude Code 风格语法 | `2026-05-27-permission-rule-args-match` |
| OtelTelemetrySink（OTLP exporter） | `2026-05-27-telemetry-otel-sink` |

未闭环（按优先级）：

- **P2** `web_search` 协议（unbound，业务侧注入后端） —— 等需求出现再做
- **❓** Memory backends / Multi-agent handoff 显式 API —— 需先决定 R1 归属，详见 [hermes-gap-roadmap.md](docs/architecture/hermes-gap-roadmap.md)

## 与同类对比

| 对比对象 | 形态 | 语言 | Taifeng 关系 |
|---|---|---|---|
| codex / Claude Code | CLI harness | Rust / TS | **范式参照** —— 抄设计不抄代码 |
| claw-code / openclaw | CLI harness | Rust / TS | **范式参照** |
| LangGraph / AutoGen / Letta | 服务端框架 | Python | **不替代** —— 它们是图 / Actor / 记忆范式 |
| LiteLLM | provider 适配层 | Python | **依赖** —— Taifeng 把它作为可选 backend |

四方范式对标的差距分析见 [docs/architecture/hermes-gap-roadmap.md](docs/architecture/hermes-gap-roadmap.md) 与 [kernel-gap-analysis.md](docs/architecture/kernel-gap-analysis.md)。

## 文档地图

| 入口 | 用途 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | AI 协作约定 + 5 条红线权威定义 |
| [AGENTS.md](AGENTS.md) | 工程协作契约（早于 CLAUDE.md，冲突时以本文件为准） |
| [docs/architecture/overview.md](docs/architecture/overview.md) | 架构总览（模块 / 数据流 / 红线 / 里程碑） |
| [docs/configurable-knobs.md](docs/configurable-knobs.md) | 业务可配置参数全清单（含 §7 structured_output） |
| [docs/architecture/hermes-gap-roadmap.md](docs/architecture/hermes-gap-roadmap.md) | hermes capability gap 对齐路线图 |
| [docs/decisions/](docs/decisions/) | 10 个 ADR 决策记录 |
| [docs/architecture/capabilities/](docs/architecture/capabilities/README.md) | 15 个能力契约（数据结构 / 协议 / 事件 / 约束的权威定义） |

## 开发工作流

能力契约驱动（contract-first）：

```bash
# 1. 先定能力契约：在 docs/architecture/capabilities/<capability>.md
#    写清数据结构 / 协议 / 事件 / 约束（数据契约 + 行为契约）

# 2. 实现 + 跑测试
PYTHONPATH=src uv run pytest tests/<相关>

# 3. 同步活文档：更新对应 docs/architecture/<module>.md

# 4. commit & push
git commit -am "feat: ..."
```

小步切片：单 commit = 一个小功能；涉及压缩 / cache / dispatch 的改动必须显式声明对 R1–R5 的影响。

## Provider 选择

```python
# 推荐：多 provider 统一适配（OpenAI / Anthropic / Gemini / 本地模型）
from taifeng.llm.providers import LiteLLMClient
client = LiteLLMClient(model="gpt-4o-mini")            # OpenAI
client = LiteLLMClient(model="anthropic/claude-3-5-sonnet")
client = LiteLLMClient(model="gemini/gemini-2.0-flash")

# 不想要 LiteLLM 依赖时：原生 OpenAI-compat
from taifeng.llm.providers import OpenAICompatClient
client = OpenAICompatClient(
    base_url="https://api.openai.com/v1",  # 也支持 vLLM / Ollama / DeepSeek
    api_key="sk-...",
    model="gpt-4o-mini",
)

# 测试 / 离线：Mock
from taifeng.llm.providers import MockClient, MockTurn
client = MockClient(turns=[MockTurn(text="hi", ...)])
```

## License

**Proprietary**（当前 `pyproject.toml` 设置）—— 暂未选择开源 license。

若计划公开发布，请先：
1. 修改 `pyproject.toml` 的 `license` 字段
2. 添加 `LICENSE` 文件（推荐 Apache 2.0 / MIT）
3. 检查 `examples/` 内是否有真实 API key 或业务数据

## 致谢

设计参考（**只抄范式，不抄代码**）：

- [openai/codex](https://github.com/openai/codex) —— Rust CLI agent，`compact.rs` / `prompt_cache.rs` / `ModelClient` 范式源头
- [Anthropic Claude Code](https://claude.com/claude-code) —— SKILL.md 范式发起者 + Hook lifecycle
- claw-code —— Claude Code 的 Rust 开源移植，tool 配对边界保护 + permission
- openclaw —— Claude Code 的 TS 开源移植，actor + session 模式
- [LiteLLM](https://github.com/BerriAI/litellm) —— 多 provider 统一适配层

---

<div align="center">

「**泰逢动天地气**」

[ADR 0001 — 为什么叫泰逢](docs/decisions/0001-naming-taifeng.md)

</div>
