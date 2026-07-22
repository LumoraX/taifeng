# Taifeng Mypy Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `PYTHONPATH=src uv run mypy src/taifeng` 从 44 个错误降到 0，同时以最小行为修复消除类型检查暴露出的运行时缺陷。

**Architecture:** 按根因而不是按文件逐条消警：先修注册表和局部注解，再修异步迭代器协议的源头，随后修真实运行时缺陷和第三方类型边界。每组独立运行定向 mypy、行为测试并提交；基础层变更全部完成后同步活文档并运行全量与真实 LLM 门槛。

**Tech Stack:** Python 3.12、mypy strict、pytest/pytest-asyncio、Pydantic v2、anyio、OpenTelemetry、uv。

---

## 文件职责映射

- `src/taifeng/loop/event.py`：EventMsg 判别联合与 kind 注册表。
- `src/taifeng/llm/client.py`、`src/taifeng/skill/registry.py`：异步迭代器协议源头。
- provider、recall/verify/handoff/turn：协议消费者，只验证连锁错误消失，不做接口重构。
- `sqlite_directory.py`、`stdio_client.py`、`apply_patch.py`、`pool.py`、`__main__.py`：类型检查暴露的真实运行时边界。
- `otel_sink.py`、`pyproject.toml`、`uv.lock`：可选依赖的公共抽象和类型 stubs。
- `docs/architecture/llm-client.md`、`docs/architecture/skill-system.md`：公开协议活文档。

### Task 1: EventMsg 注册表与低风险局部注解

**Files:**
- Modify: `src/taifeng/loop/event.py`
- Modify: `src/taifeng/context/pinned_state.py`
- Modify: `src/taifeng/loop/submission.py`
- Modify: `src/taifeng/skill/definition.py`
- Modify: `tests/loop/test_events.py`

- [ ] **Step 1: 写 EventMsg 注册表失败测试**

在 `tests/loop/test_events.py` 增加对六个遗漏 kind 的断言：

```python
def test_all_declared_resource_and_compaction_events_are_registered() -> None:
    from typing import get_args
    from taifeng.loop.event import MsgKind

    assert {
        "skill_spawn_rejected",
        "resource_limit_exceeded",
        "compaction_degradation_warning",
        "compaction_integrity_rolled_back",
        "context_budget_exceeded",
        "suspension_expired",
    } <= set(get_args(MsgKind))
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_events.py::test_all_declared_resource_and_compaction_events_are_registered -q`

Expected: FAIL，集合差集包含上述六个 kind。

- [ ] **Step 3: 最小补齐注册表和窄类型**

在 `MsgKind` 中登记六个字符串，并完成以下不改变运行时的注解：

```python
def __iter__(self) -> Iterator[PinnedStateSource]: ...
attachments: list[dict[str, Any]] = Field(default_factory=list)
frontmatter_raw: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: 验证 GREEN 与定向 mypy**

Run: `PYTHONPATH=src uv run pytest tests/loop/test_events.py -q`

Run: `PYTHONPATH=src uv run mypy src/taifeng/loop/event.py src/taifeng/context/pinned_state.py src/taifeng/loop/submission.py src/taifeng/skill/definition.py`

Expected: 测试通过，上述文件 0 errors。

- [ ] **Step 5: 提交**

```bash
git add src/taifeng/loop/event.py src/taifeng/context/pinned_state.py src/taifeng/loop/submission.py src/taifeng/skill/definition.py tests/loop/test_events.py
git commit -m "fix(types): register events and narrow core annotations"
```

### Task 2: 修正异步迭代器协议源头

**Files:**
- Modify: `src/taifeng/llm/client.py`
- Modify: `src/taifeng/skill/registry.py`
- Test: `tests/llm/test_sim_engine_integration.py`
- Test: `tests/skill/test_registry.py`

- [ ] **Step 1: 记录协议 RED**

Run: `PYTHONPATH=src uv run mypy src/taifeng/llm/client.py src/taifeng/llm/providers src/taifeng/skill/recall.py src/taifeng/skill/verify.py src/taifeng/context/strategies/handoff.py src/taifeng/loop/turn.py src/taifeng/skill/registry.py`

Expected: 包含 `Coroutine[...] has no attribute __aiter__`、provider `session` override 和 registry `watch` override。

- [ ] **Step 2: 最小修正协议签名**

```python
class ModelClientSession(Protocol):
    def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]: ...

class SkillRegistry(Protocol):
    def watch(self) -> AsyncIterator[SkillSnapshot]: ...
```

保留各实现的 `async def ...: yield`，不增加 `await`、cast 或 ModelClient 泛型参数；同时把 `__aexit__` 写为 `async def __aexit__(self, *exc: object) -> None`。

- [ ] **Step 3: 验证协议 GREEN**

Run: `PYTHONPATH=src uv run mypy src/taifeng/llm/client.py src/taifeng/llm/providers src/taifeng/skill/recall.py src/taifeng/skill/verify.py src/taifeng/context/strategies/handoff.py src/taifeng/loop/turn.py src/taifeng/skill/registry.py`

Expected: async-iterator、provider override、SimClient 结构化协议错误全部消失；剩余只允许其他任务列出的局部注解错误。

Run: `PYTHONPATH=src uv run pytest tests/llm/test_sim_engine_integration.py tests/skill/test_registry.py -q`

Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add src/taifeng/llm/client.py src/taifeng/skill/registry.py
git commit -m "fix(types): align async iterator protocols"
```

### Task 3: 修复真实运行时边界

**Files:**
- Modify: `src/taifeng/conversation/sqlite_directory.py`
- Modify: `src/taifeng/mcp/stdio_client.py`
- Modify: `src/taifeng/tool/builtins/apply_patch.py`
- Modify: `src/taifeng/loop/pool.py`
- Modify: `src/taifeng/__main__.py`
- Test: `tests/conversation/test_sqlite_directory.py`
- Test: `tests/tool/test_apply_patch.py`

- [ ] **Step 1: 写 apply-patch 权限语义失败测试**

```python
@pytest.mark.asyncio
async def test_apply_patch_requests_tool_use_permission(tmp_path: Path) -> None:
    captured: list[PermissionRequest] = []

    async def check(request: PermissionRequest) -> PermissionDecision:
        captured.append(request)
        return PermissionDecision.allow()

    policy = PermissionPolicy(default_mode="ask", prompter=CallbackPrompter(check))
    spec = make_apply_patch_tool(root_dir=tmp_path, policy=policy)
    result = await spec.handler(
        {"patches": [{"path": "new.txt", "new_text": "ok", "create": True}]},
        _ctx(),
    )
    assert not result.is_error
    assert [(request.scope, request.target) for request in captured] == [
        ("tool_use", "apply_patch")
    ]
```

- [ ] **Step 2: 运行权限测试并确认 RED**

Run: `PYTHONPATH=src uv run pytest tests/tool/test_apply_patch.py::test_apply_patch_requests_tool_use_permission -q`

Expected: FAIL，当前 scope 为 `apply_patch`。

- [ ] **Step 3: 实施五项最小修复**

```python
# sqlite_directory.py
import binascii
except (ValueError, KeyError, json.JSONDecodeError, binascii.Error) as exc:
    ...
row = cur.fetchone()
return tuple(row) if row is not None else None

# stdio_client.py
payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
def _handler_factory(mcp_name: str = name) -> ToolFunc:
    ...

# apply_patch.py
PermissionRequest(scope="tool_use", target="apply_patch", ...)

# pool.py
self._watcher: SkillFileWatcher | None = None
async def _on_change(snap: SkillSnapshot) -> None: ...
initial_history: list[ResponseItem] = []

# __main__.py
for entry in snap.entries(): ...
func: Callable[[argparse.Namespace], int] = args.func
return func(args)
```

- [ ] **Step 4: 验证行为与定向类型**

Run: `PYTHONPATH=src uv run pytest tests/conversation/test_sqlite_directory.py tests/tool/test_apply_patch.py tests/mcp -q`

Run: `PYTHONPATH=src uv run mypy src/taifeng/conversation/sqlite_directory.py src/taifeng/mcp/stdio_client.py src/taifeng/tool/builtins/apply_patch.py src/taifeng/loop/pool.py src/taifeng/__main__.py`

Expected: PASS，相关文件 0 errors。

- [ ] **Step 5: 提交**

```bash
git add src/taifeng/conversation/sqlite_directory.py src/taifeng/mcp/stdio_client.py src/taifeng/tool/builtins/apply_patch.py src/taifeng/loop/pool.py src/taifeng/__main__.py tests/tool/test_apply_patch.py
git commit -m "fix(runtime): harden typed boundary handling"
```

### Task 4: 清理剩余局部注解和 Any 泄漏

**Files:**
- Modify: `src/taifeng/llm/providers/litellm_provider.py`
- Modify: `src/taifeng/llm/providers/openai_compat.py`
- Modify: `src/taifeng/loop/turn.py`
- Modify: `src/taifeng/telemetry/console.py`

- [ ] **Step 1: 运行剩余局部 mypy RED**

Run: `PYTHONPATH=src uv run mypy src/taifeng/llm/providers/litellm_provider.py src/taifeng/llm/providers/openai_compat.py src/taifeng/loop/turn.py src/taifeng/telemetry/console.py`

Expected: 只剩 `no-untyped-def` 和 `no-any-return`。

- [ ] **Step 2: 使用真实窄类型消除错误**

```python
async def __aexit__(self, *exc: object) -> None: ...
async def _emit(self, msg: Any) -> None: ...
async def run_sub_skill(...) -> ToolResult: ...
async def _spawn_sub_runner(...) -> ToolResult: ...

def _short(value: Any, n: int) -> str:
    text = str(value)
    if len(text) <= n:
        return text
    return text[: n - 3] + "..."
```

- [ ] **Step 3: 验证并提交**

Run: `PYTHONPATH=src uv run mypy src/taifeng/llm/providers/litellm_provider.py src/taifeng/llm/providers/openai_compat.py src/taifeng/loop/turn.py src/taifeng/telemetry/console.py`

Run: `PYTHONPATH=src uv run pytest tests/loop tests/telemetry/test_console_sink.py -q`

Expected: PASS，定向 mypy 0 errors。

```bash
git add src/taifeng/llm/providers/litellm_provider.py src/taifeng/llm/providers/openai_compat.py src/taifeng/loop/turn.py src/taifeng/telemetry/console.py
git commit -m "fix(types): close remaining annotation gaps"
```

### Task 5: 第三方类型边界与 OTel

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/taifeng/telemetry/otel_sink.py`
- Test: `tests/telemetry/test_otel_sink.py`

- [ ] **Step 1: 记录第三方类型 RED**

Run: `PYTHONPATH=src uv run mypy src/taifeng/skill/loader.py src/taifeng/telemetry/otel_sink.py`

Expected: PyYAML `import-untyped` 和四个 OTel assignment 错误。

- [ ] **Step 2: 添加 stubs 并明确公共抽象**

在 `dev` extra 增加 `types-PyYAML>=6.0` 并运行 `uv lock`。OTel 使用：

```python
from opentelemetry.sdk.metrics.export import MetricExporter
from opentelemetry.sdk.trace.export import SpanExporter

span_exporter: SpanExporter
metric_exporter: MetricExporter
self._tracer_provider: TracerProvider = tp
self._meter_provider: MeterProvider = mp
```

保持导入在现有 optional-extra try/TYPE_CHECKING 边界内。

- [ ] **Step 3: 验证可选依赖行为与类型**

Run: `PYTHONPATH=src uv run pytest tests/telemetry/test_otel_sink.py -q`

Run: `PYTHONPATH=src uv run mypy src/taifeng/skill/loader.py src/taifeng/telemetry/otel_sink.py`

Expected: PASS，0 errors。

- [ ] **Step 4: 提交**

```bash
git add pyproject.toml uv.lock src/taifeng/telemetry/otel_sink.py
git commit -m "fix(types): define optional dependency boundaries"
```

### Task 6: 活文档、全仓和真实 LLM 验收

**Files:**
- Modify: `docs/architecture/llm-client.md`
- Modify: `docs/architecture/skill-system.md`
- Modify after real run: `docs/real-llm-ledger.json`
- Modify after real run: `docs/real-llm-ledger.md`

- [ ] **Step 1: 更新公开协议活文档**

`llm-client.md` 明确 `stream` 是“普通协议方法返回 AsyncIterator，具体实现可为 async generator”，具体 provider session 可协变返回；`skill-system.md` 对 `watch` 记录同一规则。

- [ ] **Step 2: 运行静态与本地全量门槛**

Run: `PYTHONPATH=src uv run mypy src/taifeng`

Expected: `Success: no issues found in 138 source files`。

Run: `PYTHONPATH=src uv run ruff check src tests`

Run: `PYTHONPATH=src uv run pytest tests/ -q`

Expected: 全部 PASS。

- [ ] **Step 3: 运行真实 LLM 零消耗自检**

Run: `PYTHONPATH=src uv run python examples/real_llm/selfcheck.py`

Expected: selfcheck PASS。

- [ ] **Step 4: 运行真实 LLM capability matrix 并更新台账**

Run: `PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py`

Expected: 所有 capability PASS，`docs/real-llm-ledger.json` 与 `.md` 记录当前基础层提交。

- [ ] **Step 5: 最终提交**

```bash
git add docs/architecture/llm-client.md docs/architecture/skill-system.md docs/real-llm-ledger.json docs/real-llm-ledger.md
git commit -m "docs: record typed protocol verification"
```

- [ ] **Step 6: 最终确认工作树**

Run: `git status --short --branch`

Expected: 分支为 `feat/session-journal-audit`，工作树无未提交文件；不 merge、不 archive。
