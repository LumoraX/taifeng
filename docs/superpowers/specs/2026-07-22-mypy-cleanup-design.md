# Taifeng 全仓 mypy 清零设计

## 背景

在 `feat/session-journal-audit` 独立 worktree 中运行：

```bash
PYTHONPATH=src uv run mypy src/taifeng
```

稳定复现 44 个错误，分布于 22 个文件。错误不是 SessionJournal 单点回归，
而是严格类型检查暴露出的全仓协议漂移、注册表遗漏、潜在运行时缺陷和普通注解债务。

## 目标与非目标

目标：

- 将 `src/taifeng` 的 mypy 错误清零，不使用宽泛 `ignore` 掩盖问题。
- 保持公开运行时行为兼容；若类型错误暴露真实缺陷，只做最小行为修复。
- 按独立根因分批修复，每批都有可观察的失败基线与回归证据。
- 保持 SessionJournal 已完成实现与验证结果不回退。

非目标：

- 不重构与错误无关的模块。
- 不归档或合并 OpenSpec change。
- 不修改业务语义，不引入宿主业务概念。

## 错误分类与处理

### 1. 类型注册及普通注解

包括裸 `dict/list`、缺失参数或返回值类型、`Any` 泄漏，以及 6 个未登记到
`MsgKind` 的事件类型。优先补齐最窄、最真实的类型，不改变数据结构或执行路径。

验证：对相关文件运行定向 mypy；EventMsg 增加序列化/判别联合回归测试，确认
新增 kind 可被当前事件模型接受。

### 2. 异步迭代器协议

`ModelClientSession.stream()` 与 `SkillRegistry.watch()` 的协议声明把返回值表达成了
`Coroutine[..., AsyncIterator[T]]`，而实现是直接可 `async for` 的异步生成器。
协议应使用普通 `def` 返回 `AsyncIterator[T]`，实现仍可使用含 `yield` 的
`async def`，从而与 Python 的异步迭代器语义一致。

验证：增加协议消费者测试，直接 `async for` 使用 stream/watch；确认无需先 await。

### 3. ModelClient session 返回类型

各 provider 返回自己的 session 具体类。协议若固定要求 `ModelClientSession`，mypy
当前拒绝这些窄返回类型，是第 2 节中 `stream()` 被错误声明成 coroutine 的连锁结果。
最小 mypy 复现已确认：把协议改为普通 `def` 返回 `AsyncIterator[T]` 后，具体 session
类可结构化满足 `ModelClientSession`，provider 的窄返回类型也可合法覆盖。因此保留
非泛型 `ModelClient`，不增加新的公开类型参数。

验证：增加静态类型契约样例并运行 provider/SimClient 集成测试，确保不加入运行时
强制转换。

### 4. 真实缺陷

逐项最小修复：

- 使用标准 `binascii` 模块而非不存在的 `sqlite3.binascii`。
- 在 `EnginePool` 初始化中声明 watcher 生命周期字段。
- 在异常块内提取稳定错误数据，避免 Python 清理异常变量后闭包继续读取。
- 将 apply-patch 权限请求明确改为 `scope="tool_use"`、`target="apply_patch"`。
  这与权限规则中的 `ApplyPatch` alias 及其他内置工具调用语义一致，避免把整组
  结构化补丁误降级成单路径 `file_write` 请求。
- 修正 MCP 响应变量复用造成的窄类型冲突。

验证：每项先增加能暴露错误行为或生命周期边界的测试，再改生产代码。

### 5. 第三方类型边界

- 在开发依赖中加入 `types-PyYAML`，同步 lockfile。
- OTel 的 `span_exporter` 显式声明为 SDK `SpanExporter`，`metric_exporter` 显式声明为
  SDK `MetricExporter`；HTTP 与 gRPC exporter 都通过对应抽象传给 processor/reader。
- `_tracer_provider` 与 `_meter_provider` 分别显式声明为 API 层 `TracerProvider` 与
  `MeterProvider`，兼容测试注入和内部构造的 SDK provider。所有 OTel 类型仍放在
  现有 optional-extra 导入边界内；未安装 extra 时模块可导入、构造 sink 才报错。

验证：分别在未启用和启用可选 OTel 依赖的类型检查路径中验证；现有 telemetry
测试不得回退。

## 实施顺序

1. 普通注解与 EventMsg 注册表。
2. 异步迭代器协议。
3. 验证具体 session 的协变返回类型及所有 provider。
4. 真实缺陷。
5. 第三方依赖与 OTel 类型边界。

每一步独立形成小提交。若某一步发现需要改变公开行为，暂停该步并重新确认设计，
不把范围扩散到后续步骤。

## 错误追踪矩阵

| 根因组 | 文件 | 定向验证 |
|---|---|---|
| 普通注解/Any | `context/pinned_state.py`, `loop/submission.py`, `skill/definition.py`, `llm/client.py`, `llm/providers/litellm_provider.py`, `llm/providers/openai_compat.py`, `mcp/stdio_client.py`, `loop/turn.py`, `telemetry/console.py`, `loop/pool.py`, `conversation/sqlite_directory.py`, `__main__.py` | 定向 mypy + 对应模块测试 |
| EventMsg 注册 | `loop/event.py` | EventMsg 模型测试 + loop 测试 |
| 异步迭代器协议 | `llm/client.py`, `skill/registry.py`, `skill/recall.py`, `skill/verify.py`, `context/strategies/handoff.py`, `loop/turn.py` | 直接 `async for` 契约测试 + 调用方测试 |
| Session 返回类型 | `llm/client.py`, `llm/providers/litellm_provider.py`, `llm/providers/openai_compat.py`, `llm/providers/gemini_provider.py`, `llm/providers/anthropic_provider.py`, `__main__.py` | 静态契约样例 + provider/SimClient 测试 |
| 真实缺陷 | `conversation/sqlite_directory.py`, `mcp/stdio_client.py`, `tool/builtins/apply_patch.py`, `loop/pool.py`, `__main__.py` | 每项边界回归测试 |
| 第三方边界 | `skill/loader.py`, `telemetry/otel_sink.py`, `pyproject.toml`, `uv.lock` | mypy + loader/telemetry 测试 |

## 验收

完成条件：

- `PYTHONPATH=src uv run mypy src/taifeng` 为 0 errors。
- 定向新增测试、相关模块测试与全量 `tests/` 通过。
- Ruff 检查通过。
- 同步公开接口活文档：`docs/architecture/llm-client.md` 记录 stream 与具体 session
  协变契约；`docs/architecture/skill-system.md` 记录 watch 的异步迭代器契约。
- 本次不新增或修改 LLM 策略类能力，因此不更新 `docs/capability-matrix.md`；若实施
  中发现必须改变策略能力，则暂停并把能力登记纳入验收。
- 因涉及 `llm/loop/context/conversation`，先运行真实 LLM selfcheck，再运行完整
  capability matrix，并在所有基础层代码提交之后更新
  `docs/real-llm-ledger.{json,md}`。
- 若真实 LLM 验证无法执行，只能报告代码与本地测试完成，不得标记整个任务完成。
