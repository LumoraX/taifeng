# ADR 0002: 用 Python 而不是 Rust / TypeScript

- 状态：Accepted
- 日期：2026-05-22

## 背景

主流 LLM agent 项目语言分布出现明显的**形态分化**：

```
CLI / 终端 harness               服务端 framework
────────────────────────         ─────────────────────────
codex          Rust              LangGraph         Python ⭐
Claude Code    TypeScript        Letta/MemGPT      Python
openclaw       TypeScript        AutoGen v0.4      Python ⭐
claw-code      Rust              Pydantic AI       Python
aider          Python (例外)     Agno              Python
                                 DSPy              Python
                                 Semantic Kernel   Py/C#/Java
                                 LiteLLM           Python
                                 Mastra            TS (例外)
```

需要在初期就锁定 Taifeng 的语言。

## 决策

**Taifeng 用 Python（3.12+），不考虑 Rust / TS。**

## 理由

### Taifeng 是服务端引擎，不是 CLI

CLI 选 Rust/TS 的 6 个硬约束在服务端**全部不成立**：

| CLI 约束 | 服务端实际 |
| --- | --- |
| 冷启动延迟 | FastAPI / Flask 常驻几天 |
| 分发体积 | Docker image 100MB 起步，没人在乎 |
| 跨平台编译 | 服务端只跑 Linux x86_64 |
| 内存常驻 | 进程驻留期内累计 LLM 调用成本远大于 runtime 内存 |
| 真并行 | agent 是 IO-bound，GIL 不是瓶颈 |
| 取消语义 | asyncio 在 Python 3.11+ 已有 `TaskGroup` / `anyio` 提供完整结构化并发 |

### 宿主项目锁死

Taifeng 第一个生产用户是 **宿主业务（Python/Flask）** 和 **另一宿主业务（Python/FastAPI）**。Rust 子模块只能通过 PyO3 桥接，**额外维护成本超过节省**。

### 生态压倒一切

LLM-adjacent Python 生态领先 Rust/TS **1–3 个月**：
- `openai` / `anthropic` / `google-genai` Python SDK 永远先发新 feature
- `tiktoken` / `sentencepiece` token counter 只有 Python 完整
- `litellm` 多 provider 适配——Python 独有
- 向量库（lance / chroma / qdrant）Python binding 最成熟

### 团队速度

> 一个工程师 3–4 周从 另一宿主业务 抽出 Taifeng（Python）  
> vs  
> 8 周用 Rust + 学 Tokio + Pin/Send/Sync

回报率不成正比。

### Python 在 IO-bound 场景的胜点

LLM 调用 99% 时间在等流式响应，不是 CPU。`httpx` + `asyncio` + `anyio.fail_after()` 已经足够。

**真正的反向胜点**：claw-code 的 `prompt_cache.rs` 是 735 行 Rust，**用 Python 实现等价语义大约 300 行**。Rust 多出来的 400 行全是类型标注、错误枚举、生命周期声明。Python 在**修改速度**上完胜。

## 不采用的情况

未来满足以下两个条件之一时，可考虑用 Rust 重写**部分核心**（不是全部）：

1. Taifeng 作为通用引擎对外分发，且要求 < 50ms 冷启动
2. 真并行执行 100+ 子 agent 且 CPU 成为瓶颈

12 个月内都用不上。**LangGraph 60k stars 还是 Python**。

## 选型补充

| 选择 | 决定 |
| --- | --- |
| Python 版本 | **3.12+**（依赖 `TaskGroup`、PEP 695 type alias、PEP 698 `@override`） |
| 异步库 | `anyio`（不是 `asyncio` 直接用）—— 跨 `asyncio` / `trio`，取消语义完备 |
| 类型检查 | `mypy --strict` + `pydantic v2` 校验运行时 schema |
| 包管理 | `uv`（参照 宿主业务 / 另一宿主业务 既定） |
| 测试 | `pytest` + `pytest-asyncio`（auto mode） |
| 格式 | `ruff format` + `ruff check`，行宽 100 |

## 已废弃的替代方案

- ❌ Rust 实现：见上文，回报率不成正比
- ❌ TypeScript 实现：宿主是 Python，不可行
- ❌ 混合语言（Python + Rust 性能核心）：维护负担过早引入
