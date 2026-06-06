# AGENTS.md —— Taifeng 工程协作约定

> 本文件供 AI agent (Claude Code / codex / claw) 在本仓库工作时遵守。

## 项目身份

**Taifeng (泰逢)**：通用 LLM Agent 微内核 / OS 调度器。
独立 infra 包，不绑定任何业务系统。第一个生产用户是 宿主业务，但**绝不引入 宿主业务 概念**。

## 工作目录

- `src/taifeng/` —— 核心实现（6 个子包：skill / tool / conversation / context / llm / loop + telemetry）
- `tests/` —— pytest（asyncio_mode=auto）
- `examples/` —— 端到端示例（mock 客户端，无需 API key）
- `docs/` —— 架构 + ADR + 调研
- `docs/architecture/capabilities/` —— 能力契约（契约先行：数据结构 / 协议 / 事件 / 约束）

## 多 Session 并发协作（一 session 一 worktree）

> 多个 AI session 并发在本仓库工作时，**绝不共用主工作树**。
> 教训（真实事故）：主工作树只有**一个共享的 HEAD / index / 工作目录**，谁 `git checkout` 切分支，就把 HEAD 从别人脚下抽走 —— 导致 commit 落错分支、别人的未提交改动被串走、被迫做 git 手术。**开分支 ≠ 隔离；独立的工作目录才隔离。** 因此"每个 session 各开分支但共用主树"恰恰是最乱的组合。

**三条硬规则：**

1. **主树只做集成**：任何 session **都不**在主树（`<repo-root>`）里做开发或 `git checkout` 切分支。主树仅用于最终 merge 或当干净参照。
2. **一 session = 一 worktree = 一分支**：session 启动即 `git worktree add .claude/worktrees/<task> -b feat/<task> <integration-point>`，全程钉死在自己的 worktree 里；**从不 `cd` 回主树、从不动别人的分支、从不切主树 HEAD**。
3. **任务范围作所有权单元**：一个 session 认领一个明确的任务范围；尽量按目录切分工（如 A 只碰 `loop/`、B 只碰 `context/`），减少重叠。

**降冲突：**

- `loop/event.py`（`MsgKind` / `Msg` Union 全局注册表）这类"全局枚举/注册表"是冲突高发区 —— 让单一 session 统一增改，或频繁从集成分支 rebase 早暴露冲突。
- 集成时**一次只合一条分支**，合完立即跑全量 `PYTHONPATH=src uv run pytest tests/` 再合下一条；不要同时合多条。

**操作前自检 + 收尾：**

- 动手提交前先 `git rev-parse --abbrev-ref HEAD` 确认在自己的 worktree 分支上。
- 合并完成后 `git worktree remove <path>` + `git branch -d <分支>`（`-d` 会校验已完全合并才删，安全）。
- 注：本仓库是 **git submodule**，worktree 建在 submodule 工作树下（`.claude/worktrees/`），不要建到父仓库去。

## 五条审 PR 红线（任何变更必须遵守）

1. **业务零侵入** —— 禁止 `tenant_id` / 业务术语 / 宿主业务 模块 import
2. **Cache 友好** —— 压缩必须返回 `cache_invalidated: bool` + `anchor_preserved_until: int`
3. **可观测** —— turn / tool / skill / compaction / cache_break 都必须有 EventMsg
4. **可取消** —— 长时操作接收 `CancellationToken`；不允许阻塞主 actor
5. **可 resume** —— 默认实现是 JSONL 追加写；其他 store 用 `MessageStore` 协议

## 实现约束

- Python 3.12+，类型注解全部 `from __future__ import annotations`
- 异步用 `anyio`（必要时回退 `asyncio`），不用同步阻塞调用
- 数据类用 `@dataclass(frozen=True)` 或 `pydantic.BaseModel`
- 文件 ≤ 800 行硬红线；函数 ≤ 80 行；圈复杂度 ≤ 10
- 注释中文（覆盖默认 no-comments 规则）；module/class/function 必须有 docstring
- 配置走依赖注入；禁止 `os.getenv` 在 src/ 内

## 测试约束

- 新模块必须有 `tests/test_<module>.py`
- LLM 调用走 `MockClient` —— 不能在 CI 里调真实 API
- 文件 IO 走 `tmp_path` fixture
- 边界必测：cancel、空输入、超长 body、环检测、深度上限

## 命令速查

```bash
# 安装
uv venv && uv pip install -e ".[dev,litellm]"

# 测试（全套）
PYTHONPATH=src uv run pytest tests/ -v

# 示例（basic/ 与各 pattern demo 走 MockClient，无需 API key；real_llm/ 需真实 key）
PYTHONPATH=src uv run python examples/basic/minimal_chat.py
PYTHONPATH=src uv run python examples/basic/composite_skill.py
PYTHONPATH=src uv run python examples/orchestration/demo.py   # 声明式编排
PYTHONPATH=src uv run python examples/mcp_basic/demo.py       # taifeng 作为 MCP server
# 完整清单见 examples/ 各子目录（instructions_basic / skill_with_script / research_assistant /
#   travel_planner / code_review / mcp_hitl / permission / persistence / web_ui ...）

# CLI
PYTHONPATH=src uv run python -m taifeng skill list /path/to/skills
PYTHONPATH=src uv run python -m taifeng skill validate /path/to/skills
```

## 能力契约工作流（contract-first）

1. 定契约：在 `docs/architecture/capabilities/<capability>.md` 写清数据结构 / 协议 / 事件 / 约束
2. 实现：小步切片，每步完成立即 commit；红测试不可跳过
3. 同步：更新对应 `docs/architecture/<module>.md` 活文档

## 文档义务

`docs/README.md` 是文档索引 + 分类约定（权威）。两类文档处理方式不同，别混用：

- `docs/architecture/` = 当前架构**活文档**（含 `capabilities/` 契约层）→ 改了 `src/` 设计 / 数据流就**更新**对应篇（§编号对应：skill→skill-system、loop+tool→agent-loop、conversation、context→context-compression、llm→llm-client、切分→overview）。永远代表现状，**不归档、不堆废弃史**。
- `docs/decisions/` = ADR → **只增不改**，推翻写新 ADR 标 `Supersedes #NNNN`。

**判据**：写"现状"进 architecture（模块篇或 `capabilities/` 契约），写"决策 / 为什么"进 ADR。
**硬约束**：实现完成但 architecture 未同步 → 不得 archive，PR 不合并。

## 参照实现

代码参照三个开源项目（位于本机 `<opensource>/`）：

- **codex** (Rust) —— 范式权威。`codex-rs/core/src/compact.rs` / `client.rs` 是 cache-aware + handoff 的源头
- **claw-code** (Rust) —— Claude Code 开源移植。`crates/runtime/src/compact.rs` 含 tool 配对边界保护
- **openclaw** (TS) —— `src/agents/*` 提供 actor + session 模式

移植到 Python 时**只学范式，不抄代码**（语言习惯不同）。


<claude-mem-context>
# Memory Context

# claude-mem status

This project has no memory yet. The current session will seed it; subsequent sessions will receive auto-injected context for relevant past work.

Memory injection starts on your second session in a project.

`/learn-codebase` is available if the user wants to front-load the entire repo into memory in a single pass (~5 minutes on a typical repo, optional). Otherwise memory builds passively as work happens.

Live activity: http://localhost:37777
How it works: `/how-it-works`

This message disappears once the first observation lands.
</claude-mem-context>