# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 配套文件：`AGENTS.md` 是给所有 AI agent 看的工程协作约定；本文件聚焦 Claude Code 在本仓库执行任务时需要的快速上下文。两者冲突时以 `AGENTS.md` 为准（它是更早的工程契约）。

## 项目身份

**Taifeng (泰逢)** —— 通用 LLM Agent **微内核 / OS 调度器**（不是织造工具，不是业务框架）。Python 3.12+，对标 codex (Rust) / Claude Code (TS) / claw-code (Rust) 的 CLI agent 范式，为 Python 服务端提供可嵌入的 agent 引擎。

**关键定位**：
- **LLM OS 的内核：用来开发开箱即用的产品，自身不是产品**。立项裁决四规则见 ADR 0017——①内核机制缺口→做；②模型认知回路原语（自我 review / 任务清单工作记忆 / 状态穿越压缩）→做；③外部成熟服务能承担的（DB / 向量记忆 / 知识库）→内核只定协议、实现走外部；④仅"别家有"的产品功能→不做。
- 是独立 infra 包，**与业务完全解耦**。`src/` 内**禁止出现任何业务概念**（无 tenant / 无领域名词 / 无 LLM provider lock-in）。
- 不与 LangGraph / AutoGen / Letta 竞争（不同范式：codex 风格 vs 图/Actor/记忆）。
- 范式核心：**skill 是 markdown**（不是 function tool）、**LLM 是调度器**（不是被调度对象）、**压缩 cache-aware**、**Actor 风格 Submission/EventMsg 双总线**。

## 常用命令

```bash
# 安装（uv 是必须，不用 pip）
uv venv && uv pip install -e ".[dev,litellm]"
# telemetry-otel 是可选 extra；不装则 tests/telemetry/test_otel_sink.py 自动跳过
# （importorskip 保护），要真跑 OTel 那组测试再加：
#   uv pip install -e ".[dev,litellm,telemetry-otel]"

# 跑全部测试（必须带 PYTHONPATH=src，因为是 src-layout）
PYTHONPATH=src uv run pytest tests/ -v

# 跑单个测试文件
PYTHONPATH=src uv run pytest tests/test_engine_e2e.py -v

# 跑单个测试
PYTHONPATH=src uv run pytest tests/test_dispatch.py::test_circular_reference_detection -v

# 端到端示例 —— **不是所有 demo 都免 key**。分档判据是「是否引用 examples/_provider_bootstrap」
# （不是「是否调 build_model_client」—— mcp_basic / mcp_hitl 把 key 经环境变量传给
#   spawn 出的 `taifeng mcp serve` 子进程，并不直接构造 client）。
# 权威分档由脚本给出，别照抄下面的手工清单：
#   python scripts/verify_examples.py --list   # 看分档
#   python scripts/verify_examples.py          # 跑全部 sim 档并核验输出
# ① 纯 SimClient，无需 API key
PYTHONPATH=src uv run python examples/basic/minimal_chat.py
PYTHONPATH=src uv run python examples/basic/composite_skill.py
PYTHONPATH=src uv run python examples/basic/instructions_basic.py   # 指令分层注入 + 热更
PYTHONPATH=src uv run python examples/basic/skill_with_script.py    # SKILL.md scripts 运行时
PYTHONPATH=src uv run python examples/orchestration/demo.py         # 声明式编排（parallel/serial/when）
PYTHONPATH=src uv run python examples/multi_expert_consult/demo.py  # detached spawn + join-barrier
PYTHONPATH=src uv run python examples/turn_rewind/demo.py           # 节点回访 re_reason / retry_tool
#   其余同类：audit_observability / compression_showcase / concurrent_fanout / doom_loop /
#   hooks_showcase / kernel_knobs / mcp_showcase / memory / peer_messaging /
#   permission_grants / post_turn_review / read_skill_lazy / skill_outcome_fleet /
#   step_pipeline / subagent_isolation / suspend_resume
#
# ② 需要真实 LLM key（脚本自读 .env 的 LLM_BOOTSTRAP_*）
#   code_review / dual_track / mcp_basic / mcp_hitl / numeric_loop / product_review /
#   research_assistant / selective_approval / travel_planner —— 见 examples/<name>/demo.py
#   web_ui/server.py 与 step_pipeline/server.py 同样需 key（常驻 HTTP 服务，不自行退出）
PYTHONPATH=src uv run python examples/real_llm/e2e.py               # real_llm/ 下均需 key
#   ⚠️ 端点若拒 role=system（报 System messages are not allowed），用
#      LLM_BOOTSTRAP_PROVIDER=codex 覆盖 .env 的 provider 再跑
#
# ③ 无 demo.py 的目录（别照 <name>/demo.py 找）：
#   observability/audit_index_hook.py、permission/web_prompter.py、
#   persistence/{postgres,redis}_thread_directory.py、form_hitl（只含 skills/，无入口脚本）
#
# ⚠️ 多数 demo 末尾无条件 return 0 —— **退出码 0 不等于跑通**，必须看输出里有没有
#    [TURN ✗] / isError=True / ❌ 之类的带内失败标记

# CLI（用于排查 SKILL.md 目录）
PYTHONPATH=src uv run python -m taifeng skill list <skills_dir>
PYTHONPATH=src uv run python -m taifeng skill show <skills_dir> <skill_id>
PYTHONPATH=src uv run python -m taifeng skill validate <skills_dir>
PYTHONPATH=src uv run python -m taifeng engine demo <skills_dir> <entry_id> -m "..."

# Lint / 类型检查（pyproject.toml 已配 ruff + mypy strict）
# 门禁只卡 F 类（已全仓清零）；全量 ruff 尚有 300+ 条既有欠账（E501/TC/I 为主），别被吓到
uv run ruff check --select F src tests examples scripts   # ← 门禁跑的就是这条
uv run ruff check src/ tests/                             # 全量（含既有欠账）
# mypy 必须带上可选 extra，否则 opentelemetry 未安装会报 12 条 import-not-found 假阳
uv run --extra dev --extra litellm --extra telemetry-otel mypy src/
```

`pytest.ini_options.asyncio_mode = "auto"` —— 所有 async 测试函数无需 `@pytest.mark.asyncio` 装饰。

## 完成定义（DoD）

标记 task 完成、回报"已完成"、或提交 commit 前必须做到：
1. **跑通验证命令**：相关 `pytest tests/test_<x>.py` 全绿，或对应 example 端到端无异常。
2. **复述实际命令 + 关键输出**：不是"应该 OK"，而是贴命令与输出。
3. 红测试**禁止**以 "pre-existing" 为借口跳过。先排查是否与本次改动相关。

CI 门禁（`.github/workflows/ci.yml`，push main / 每个 PR 自动跑）会做同样三件事：
Python 3.12+3.13 全量 `pytest tests/`、`scripts/verify_examples.py` 的 examples 冒烟、
`ruff check --select F src tests examples scripts`。本地跑这三条就等于预跑了门禁。**门禁不跑真实 LLM**——真实回归仍是
人工跑 `examples/real_llm/capability_matrix.py` 并提交台账（见「测试约束」）。

## 多 Session 并发协作

多个 session 并发在本仓库工作时**绝不共用主工作树**——主树只有一个共享 HEAD，谁切分支就把 HEAD 从别人脚下抽走，会导致 commit 落错分支 / 别人未提交改动被串走（**开分支 ≠ 隔离，独立工作目录才隔离**）。规则：

1. **主树只做集成**，不在主树开发或 `git checkout` 切分支；
2. **一 session 一 worktree 一分支**：`git worktree add .claude/worktrees/<task> -b feat/<task> <integration-point>`，全程钉死其中，不 `cd` 回主树、不动别人分支；
3. **一个 session 认领一个明确的任务范围**，按目录切分工降冲突（`loop/event.py` 等全局注册表是冲突高发区）；
4. 集成**一次合一条**分支 + 跑全量 `PYTHONPATH=src uv run pytest tests/`；收尾 `git worktree remove` + `git branch -d`。

> 完整约定（含真实事故教训、submodule 注意点）见 `AGENTS.md` 「多 Session 并发协作」节。

## 五条审 PR 红线（任何变更必须遵守）

| # | 红线 | 落实方式 |
| --- | --- | --- |
| **R1 业务零侵入** | `src/` 内禁止业务概念：`tenant_id`、`audience`、领域名词（无论中英文）、业务子模块路径 | 业务侧通过 `AgentPolicy` 钩子注入策略 |
| **R2 Cache 友好** | 压缩动作必须返回 `CompressionResult { cache_invalidated: bool, anchor_preserved_until: int }` | mid-turn 只改 tail；pre-turn 才允许动 head |
| **R3 可观测** | 关键路径必须打 `EventMsg`：`turn_started` / `tool_dispatched` / `compaction_attempted` / `cache_break_detected` / `provider_retry` | 通过 `TelemetrySink` 协议，不绑定后端 |
| **R4 可取消** | 长时操作必须接收 `CancellationToken`；子 agent 通过 `cancel.child()` 派生 | 不允许阻塞主 actor |
| **R5 可 resume** | 默认 store 是 JSONL 追加写；业务侧落 DB 自行实现 `MessageStore` 协议 | `MessageStore` 在 `conversation/store.py` |

## 实现约束（src/ 内强制）

- **Python 3.12+**，所有模块顶部 `from __future__ import annotations`
- **异步**用 `anyio`（必要时回退 `asyncio`），不写同步阻塞 IO
- **数据类**用 `@dataclass(frozen=True)` 或 `pydantic.BaseModel`
- **配置**通过依赖注入；**`src/` 内禁止 `os.getenv`**（业务侧读环境变量后传入构造函数）
- **文件 ≤ 800 行**硬红线，警戒线 500；**函数 ≤ 80 行**；圈复杂度 ≤ 10
- **中文注释**（覆盖默认 "no comments" 规则）：所有 module / class / function 必须有 docstring；关键逻辑块行内中文注释
- **错误**：分类到既有 `LLMError` / `DispatchVerdict` 子类，禁止 silent fallback（`except: pass`、`data.get('x', 默认值)`）

## 测试约束

- 新模块必须有对应 `tests/test_<module>.py`
- LLM 调用走 `SimClient`（conformance 模拟器）—— **CI 内禁止调用真实 API**（`tests/` 全部用 sim；真实 LLM 验证只在 `examples/real_llm/`，结果落 `docs/real-llm-ledger.md` 台账）
- **真实回归红线**：凡变更基础层（`src/taifeng/{llm,loop,context,conversation}/`），合入前必须全量跑 `PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py` 并提交更新后的 `docs/real-llm-ledger.{json,md}`；台账 commit 落后于基础层变更 → 不得标 task 完成 / openspec archive。烧 key 前可先 `examples/real_llm/selfcheck.py`（sim 干跑，零消耗）
- **能力登记红线**：新增 / 修改 LLM 策略类能力必须同步登记 `docs/capability-matrix.md`（含「真实 LLM 验证」列），与「architecture 未同步 → PR 不合并」同级
- 文件 IO 走 `tmp_path` fixture，不写仓库内固定路径
- **边界必测**：cancel / 空输入 / 超长 body / 环检测 / 深度上限 / 并发

## 架构总览

详见 `docs/architecture/overview.md`。一张图速记：

```
src/taifeng/
├── skill/        # §1.1 SkillDefinition (atomic/composite) / loader / registry / dispatch / 环检测 / FileWatcher
├── tool/         # §1.2 tool 部分：ToolSpec (parallel_safe) / Runtime（RwLock 并行调度）/ builtins
├── conversation/ # §1.3 ResponseItem / MessageStore 协议 / JsonlMessageStore + SQLite 旁路索引
├── context/      # §1.4 ContextBudget / CompressionStrategy 协议 / Handoff + Sliding 策略 / cache_stats
├── llm/          # §1.5 ModelClient 协议 / ResponseEvent / retry / providers (litellm / openai_compat / mock)
├── loop/         # §1.2 主循环：Submission/Op + EventMsg + Engine（主 actor）+ TurnRunner + Pool + Cancellation
├── hooks/        # PreToolUse / PostToolUse / PreCompact / PreTurn（claw-code 范式）
├── permission/   # HITL 审批：PermissionPolicy + Rule + Prompter（CLI / Callback）
├── mcp/          # MCP stdio client（连外部 MCP server 自动注册 tools）
└── telemetry/    # ConsoleSink + JsonlSink（其他后端业务侧自接）
```

按 ADR 0006 **统一为 Skill 抽象** —— 没有独立的 `agent/` 包，skill-to-skill 派发归 `skill/dispatch.py`，composite skill 替代 agent 概念。

### 一次 turn 的数据流（速记）

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

## 关键抽象（找代码用）

| 你想做 / 改 | 看这里 |
| --- | --- |
| SKILL.md 字段、frontmatter 校验 | `src/taifeng/skill/definition.py` + `loader.py` |
| call_skill 派发 / 深度环检测 | `src/taifeng/skill/dispatch.py` |
| 内置工具 (read_skill / call_skill / file_read / file_write / shell_exec) | `src/taifeng/tool/builtins/` |
| 压缩策略 (handoff / sliding) | `src/taifeng/context/strategies/` |
| 多 provider 适配 | `src/taifeng/llm/providers/` |
| 主循环 / Engine / Pool | `src/taifeng/loop/engine.py` + `turn.py` + `pool.py` |
| 业务可配置参数全清单 | `docs/configurable-knobs.md`（构造时参数 + 运行时 Op + Engine 公开属性）|
| 公共 API 一览 | `src/taifeng/__init__.py` 的 `__all__` |

## 能力契约工作流（contract-first）

每个能力的**稳定契约**（数据结构 / 协议 / 事件 / 约束）落在 `docs/architecture/capabilities/<capability>.md`，索引见 [`docs/architecture/capabilities/README.md`](docs/architecture/capabilities/README.md)。

```
docs/architecture/capabilities/<capability>.md   # 能力契约（数据契约 + 行为契约）
docs/architecture/<module>.md                     # 模块叙述活文档（如何协作）
docs/decisions/NNNN-*.md                          # ADR（为什么这么定）
```

工作流：先定/更新能力契约 → 小步实现（每步完成即 commit，≤ 3h）→ 同步对应 `architecture/<module>.md` 活文档。涉及压缩 / cache / dispatch 的改动 **必须显式声明对 R1–R5 的影响**。

## 文档体系与义务

> 文档索引与分类约定的**权威**在 `docs/README.md`。下表是速查——四类文档**寿命不同、处理方式不同，禁止混用**：

| 目录 | 是什么 | 设计 / 逻辑变更后怎么处理 |
| --- | --- | --- |
| `docs/architecture/` | **当前生效的架构设计**（活文档，含 `capabilities/` 契约层） | **更新**对应模块篇 / 契约，永远代表现状；**不归档、不堆废弃史** |
| `docs/decisions/` | ADR 决策记录（为什么这么定） | **只增不改**；要推翻写新 ADR 标 `Supersedes #NNNN` |

**判据**：这条信息是"系统现在的样子" → 改 architecture（模块篇或 `capabilities/` 契约）；是"某次决策的经过 / 为什么" → 记 ADR，**不往 architecture 堆废弃史**（例：砍掉某 Op 的理由进 ADR，architecture 只写"现在有哪几种 Op"）。

改了 `src/` 模块的设计 / 数据流，必须同步**对应** architecture 篇（§编号一一对应：`skill/`→skill-system、`loop/`+`tool/`→agent-loop、`conversation/`→conversation、`context/`→context-compression、`llm/`→llm-client、模块切分→overview）。

**硬约束**：实现完成但 architecture（模块篇 / 契约）未同步 → PR 不合并（同 `docs/README.md` 维护红线）。

## 参照实现

设计范式参照 `<opensource>/` 下三个开源项目（**只学范式，不抄代码**，语言习惯不同）：

- **codex** (Rust) —— `codex-rs/core/src/{compact.rs, client.rs, session/*}`：cache-aware + handoff 源头
- **claw-code** (Rust) —— `crates/{runtime, api}/src/*`：tool 配对边界保护、hooks、permission
- **openclaw** (TS) —— `src/agents/* + src/context-engine/*`：actor + session 模式

不抄业务概念。所有移植后的 Python 文件必须有「参照 X，差异 Y」的注释或 ADR 说明。

## 语言要求

- 文档、注释、commit message、PR 描述：**中文**
- 变量 / 函数 / 类名：**英文**（遵循 PEP 8 与社区惯例）
- 与用户沟通：中英文均可，看用户偏好
