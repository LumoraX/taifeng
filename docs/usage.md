# 使用指南

> M1–M3 已实现的真实 API。

## 安装

```bash
cd taifeng
uv venv
uv pip install -e .                 # 核心
uv pip install -e ".[litellm]"      # + LiteLLM provider
uv pip install -e ".[dev]"          # + 测试工具
```

## 三层使用粒度

### A. 单 Engine（脚本 / 测试）

```python
import asyncio
import taifeng
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage
from taifeng.tool import ToolRegistry
from taifeng.tool.builtins import make_read_skill_tool, make_call_skill_tool

async def main():
    registry = await taifeng.FilesystemSkillRegistry.load("/data/skills")
    store = taifeng.JsonlMessageStore("/data/threads")

    tools = ToolRegistry()
    tools.register(make_read_skill_tool())
    tools.register(make_call_skill_tool())

    client = MockClient(turns=[MockTurn(text="hello", usage=TokenUsage())])

    entry = registry.get("code-reviewer")
    thread_id = await store.create_thread(entry_skill_id=entry.id)

    engine = taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=registry.snapshot(),
        tool_runtime=taifeng.ToolCallRuntime(tools),
        model_client=client,
        store=store,
        thread_id=thread_id,
    )
    # ... submit + subscribe
```

### B. EnginePool（生产服务）

```python
from taifeng.llm.providers import LiteLLMClient

# 进程启动时一次
pool = await taifeng.EnginePool.create(
    skills_dir="/data/skills",
    threads_dir="/data/threads",
    model_client=LiteLLMClient(
        model="claude-sonnet-4-6",
        api_key=settings.LLM_KEY,
    ),
)

# 每会话
engine = await pool.get_or_create(
    session_id="sess-abc",
    entry_skill_id="code-reviewer",
)
sub_id = await engine.submit(taifeng.UserMessage(text="..."))
async for ev in engine.subscribe(sub_id):
    if ev.msg.kind in ("turn_completed", "turn_failed"):
        break
```

### C. 加 ConsoleSink 观测

```python
from taifeng.telemetry import attach_console_sink

sink_task = attach_console_sink(engine, color=True)
# 控制台自动打印 turn / tool / skill / cache / compaction 事件
```

## SKILL.md 编写

### Atomic skill

```markdown
---
name: style-checker
description: 代码风格规则集
version: 1.0.0
type: atomic
---
# 风格规则集
函数 ≤ 80 行；命名走 snake_case...
```

### Composite skill (entry)

```markdown
---
name: code-reviewer
description: 代码审查专家 —— 协调子 skill 完成多维度审查
version: 1.0.0
type: composite
entry: true
model: claude-sonnet-4-6
child_skills: [style-checker, security-scanner]
tool_names: [file_read, http_request]
max_call_depth: 6
---
# 代码审查专家
...
```

## EventMsg 订阅

```python
async for ev in engine.subscribe(sub_id):
    match ev.msg.kind:
        case "assistant_text":
            sse_send(ev.msg.data["delta"])
        case "tool_call_completed":
            log_tool(ev.msg.data)
        case "turn_completed":
            log_usage(ev.msg.data["usage"])
            break
```

完整事件列表见 [架构：主循环](architecture/agent-loop.md)。

## 常见模式

### Cancel 一个 turn

```python
await engine.submit(taifeng.loop.CancelTurn(submission_id=running_sub_id))
```

### 注入业务 system 消息

```python
await engine.submit(
    taifeng.loop.InjectSystemMessage(
        text="用户已升级到 pro tier，可以使用 oncology-deep-analysis",
        source="subscription",
    )
)
```

### 手动触发压缩

```python
await engine.submit(taifeng.loop.CompactNow())
```

### Shutdown

```python
await engine.shutdown()
# 或在 pool 上：
await pool.release(session_id)
await pool.close()
```

## 业务侧实现 InstructionSource

> 把"系统指令文本"（类似 codex 的 `AGENTS.md`）作为多层、可热更、可外部读取的资源注入 engine —— 但**协议化**：库不读文件 / 不读环境 / 不假设 cwd。详见 ADR 0007 与 `docs/architecture/agent-loop.md` §1.6。

### 三档 scope：engine / session / turn

```python
import taifeng
from taifeng import InstructionLayer, InstructionContext, InstructionSource

class TenantPolicySource:
    """业务侧：按租户读取合规策略文本。

    ⚠️ 红线：fetch 内 SHALL NOT 自行发起 HITL 询问（弹窗 / SSE / Slack bot）。
    动作级 HITL 由 PermissionPolicy + PermissionPrompter 统一承担；
    数据级权限不足直接 raise 或返回 None（详见 spec D6）。
    """

    def __init__(self, db) -> None:
        self._db = db

    async def fetch(self, ctx: InstructionContext) -> str | None:
        # 业务侧自决：从 DB / 配置中心 / S3 / etc. 拉
        # 领域上下文（如租户）走开放 metadata，taifeng 不解析（R1）
        row = await self._db.policies.get(tenant=ctx.metadata.get("tenant"))
        if row is None:
            return None  # 本次不注入
        if not row.allowed_for(ctx.session_id):
            return None  # 数据级权限不足：直接返回 None，不发 HITL
        return row.text  # 业务自决格式（Markdown / XML / plain text）


pool = await taifeng.EnginePool.create(
    skills_dir=skills_dir,
    threads_dir=threads_dir,
    model_client=client,
    instruction_layers=[
        # engine scope：进程级，启动一次性 resolve
        InstructionLayer(
            name="global-policy",
            source="你必须始终用中文回答用户。",
            scope="engine",
            priority=10,
        ),
        # session scope：engine 实例缓存，可通过 UpdateInstructions 热更
        InstructionLayer(
            name="tenant-policy",
            source=TenantPolicySource(db=my_db),
            scope="session",
            cache_ttl_seconds=600,  # 10 分钟内复用
            priority=50,
        ),
        # turn scope：每次 turn 启动前 resolve
        InstructionLayer(
            name="trace",
            source=TraceSource(),
            scope="turn",
            cache_ttl_seconds=0,    # 每个 turn 必拉
            cache_volatile=True,    # 业务侧明确知道这破 cache
            priority=100,
        ),
    ],
)
```

### 装配位置

```xml
<system_instructions name="global-policy" scope="engine">
你必须始终用中文回答用户。
</system_instructions>

<system_instructions name="tenant-policy" scope="session">
本租户合规策略：禁止涉及 X / Y / Z 话题。
</system_instructions>

<system_instructions name="trace" scope="turn">
trace_id=abc-123
</system_instructions>

<entry_skill ...>...</entry_skill>
<available_child_skills>...</available_child_skills>
<dispatch_policy>...</dispatch_policy>
```

### 运行时热更（UpdateInstructions Op）

```python
import taifeng

# 例：A/B 实验切换 persona
await engine.submit(
    taifeng.UpdateInstructions(
        layer_name="tenant-policy",
        new_source="本租户切换到新 persona：温柔耐心 + 主动追问。",
    ),
)
# 下一个 turn 立即生效（缓存自动失效）。
# 未知 layer_name → 发 instruction_update_rejected 事件，不会污染 layers。
```

### 外部读取最近一次 resolve 快照

```python
snap = engine.instructions_snapshot()  # list[ResolvedInstruction] 副本（frozen）
for item in snap:
    print(item.name, item.scope, item.source_kind, item.cache_hit, len(item.text))
```

### 失败处理（fail-fast）

`InstructionSource.fetch` 抛任何异常 → 包成 `InstructionFetchError` → 该 turn 失败（发 `turn_failed` 事件）。**禁止** silent fallback 到空字符串（指令文本可能含合规约束，回退到空 = 严重违规）。

```python
async for ev in engine.subscribe_all():
    if ev.msg.kind == "instruction_fetch_failed":
        # data = {layer_name, cause_repr}
        sentry.capture_message(f"instruction fetch failed: {ev.msg.data}")
    if ev.msg.kind == "instruction_updated":
        # data = {layer_name, new_source_kind}
        print(f"layer {ev.msg.data['layer_name']} hot-swapped")
```

### Cache 友好（cache_volatile）

`scope='turn'` + `cache_ttl_seconds=0` 的 layer 每 turn 都拉，会导致 prompt cache miss。`InstructionLayer.cache_volatile=True` 显式标注，业务侧可在 `engine.instructions_snapshot()` 看到此标志，结合 `cache_break_detected` 事件追踪 cache miss 根因。

> 频繁变动的指令应优先考虑放进消息 metadata 或 user message，而不是 system layer。

## Web prompter 实现（permission gate · ADR 0010）

业务侧把 `PermissionPolicy + CallbackPrompter` 接入 Web SSE / WebSocket
做人机审批。重点是利用 typed PermissionRequest 字段渲染 UI、并显式设置
`prompter_timeout_seconds` 防止前端卡死阻塞 turn。

### 最小骨架

```python
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)


# 1. 业务侧 prompter —— 推 SSE 给前端，等回调 future
async def web_prompter(request: PermissionRequest) -> PermissionDecision:
    # request 字段全部 typed —— 不需要 metadata.get('xxx', '默认') 防御
    payload = {
        "scope": request.scope,
        "target": request.target,
        "thread_id": request.thread_id,
        "submission_id": request.submission_id,
        "entry_skill_id": request.entry_skill_id,
        "call_chain": list(request.call_chain),
        "turn_index": request.turn_index,
        "metadata": request.metadata,  # 业务侧领域上下文（如租户 id）全在这里；taifeng 不解析
    }
    # 推到前端 SSE 频道（按 thread_id 路由）
    fut = asyncio.get_event_loop().create_future()
    await sse_publish(channel=request.thread_id, event="permission_required", data=payload, future=fut)
    # 等前端回 PermissionDecision（业务 HTTP endpoint 写 fut.set_result(...)）
    return await fut


# 2. telemetry 回调 —— 把超时事件推到业务监控
async def my_telemetry(kind: str, payload: dict) -> None:
    # 例如：prometheus counter / sentry / 业务 audit log
    metrics.increment(f"permission.{kind}", tags=payload)


# 3. 构造 PermissionPolicy
policy = PermissionPolicy(
    rules=[
        # 业务侧 child skill 黑名单示例：禁止 dispatch 到某个高成本 skill
        PermissionRule(
            scope="skill_dispatch",
            target_pattern="expensive-deep-analysis",
            mode="deny",
            reason="blocked_by_policy",
        ),
    ],
    default_mode="ask",
    prompter=CallbackPrompter(web_prompter),
    prompter_timeout_seconds=60.0,  # 1 分钟未响应自动 deny
    telemetry=my_telemetry,
)


# 4. 接入 TurnRunner（pool 注入 / 业务自己构造 TurnRunner）
runner = TurnRunner(
    entry_skill=entry,
    ...,
    permission_policy=policy,   # ADR 0010 新增字段
    request_metadata={"tenant": "tenant-7"},  # 业务侧不透明上下文，合并进 metadata（taifeng 不解析）
)
```

### 完整可运行示例

参见 `examples/permission/web_prompter.py`：3 个端到端案例（规则 allow / 规则 deny / prompter timeout）。

### 关键约束

- **`prompter_timeout_seconds` 生产必填**：默认 0 = 不超时，业务忘了配
  + prompter 实现忘了加 timeout → engine 永久阻塞 turn cancel 都救不出来
- **prompter 内必须可取消**：`anyio.fail_after` 会取消 prompter 内的 await；
  prompter 实现必须遵守 anyio cancel scope（asyncio.Future 默认遵守）
- **typed 字段读取无需兜底**：旧代码 `request.metadata.get('thread_id', '')` →
  新写法 `request.thread_id`（默认空串而不是 None；业务侧可直接渲染）

---

## 持久化层（store-protocol-decoupling）

### 默认 SQLite 索引 —— 零配置

```python
import taifeng

pool = await taifeng.EnginePool.create(
    skills_dir=Path("./skills"),
    storage_dir=Path("./data"),       # JSONL 主存 + 自动 SqliteThreadDirectory 索引
    model_client=...,
)
```

- 主存：`./data/threads/<thread_id>.jsonl`（一 thread 一文件，首行 `__meta__`）
- 索引：`./data/taifeng-index.db`（stdlib sqlite3 + WAL）
- `engine.list_threads()` 查 SQLite；`load_history(tid)` 读 JSONL

### 业务侧换 Redis ThreadDirectory

```python
from examples.redis_thread_directory import RedisThreadDirectory

directory = RedisThreadDirectory(url="redis://prod-cluster:6379")
pool = await taifeng.EnginePool.create(
    skills_dir=...,
    storage_dir=Path("./data"),       # JSONL 仍走本地
    thread_directory=directory,       # 元数据 + list 查询走 Redis
    model_client=...,
)
```

业务方对 4 个 `ThreadDirectory` 方法的实现完全自主（用什么 client / pool / 路由策略）。
主存 JSONL 不变 —— 即使 Redis 整个崩了，`rebuild_index(writer, new_directory)` 能从 JSONL 恢复元数据。

### 实现 IndexHook 投递审计

```python
from datetime import datetime, UTC
import json

class AuditLogHook:
    def __init__(self, path: Path):
        self._path = path
    async def on_thread_created(self, meta):
        await self._append({"event": "thread_created", "tid": meta.thread_id})
    async def on_message_appended(self, thread_id, items):
        await self._append({"event": "append", "tid": thread_id, "count": len(items)})
    async def on_metadata_updated(self, thread_id, patch):
        await self._append({"event": "update", "tid": thread_id, "keys": list(patch)})
    async def _append(self, payload):
        payload["timestamp"] = datetime.now(UTC).isoformat()
        # 生产环境用 aiofiles 或 logging handler
        with open(self._path, "a") as f:
            f.write(json.dumps(payload) + "\n")

pool = await taifeng.EnginePool.create(
    skills_dir=...,
    storage_dir=...,
    model_client=...,
    index_hook=AuditLogHook(Path("/var/log/taifeng-audit.log")),
)
```

- fire-and-forget：hook 调用不阻塞 turn
- hook 抛异常 → 发 `index_hook_failed` 事件（要订阅 `sink` 才看得到）
- `engine.shutdown()` 5s grace period 后未完成 hook → cancel + 发 `index_hook_abandoned`

完整示例：`examples/observability/audit_index_hook.py`（端到端可跑，3 个 turn → 7 行 audit log）。

### 何时换什么

| 业务需求 | 实现什么 | 估计 LOC |
| --- | --- | --- |
| 单机 / 中小规模 | 用默认 | 0 |
| 加速 list / 多机共享元数据 | `ThreadDirectory` (Redis/PG) | ~30-80 |
| 审计 / 投递 ES / Kafka / 异步 metric | `IndexHook` | ~10-50 |
| 主存必须落 PG / 合规要求 | `MessageWriter` + `ThreadDirectory` | ~150-300 |

详见 ADR `docs/decisions/0008-store-protocol-decoupling.md` 与架构文档 `docs/architecture/conversation.md`。

---

## SKILL.md scripts 与 run_script（scripts-runtime · ADR 0009）

SKILL.md 中 `scripts:` 声明的脚本通过内置 `run_script` 工具暴露给 LLM 执行。9 阶段流程含 `PermissionPolicy(scope='script_exec')` + `pre/post_script_use` hook 双重门控。

### 最小落地

```python
from taifeng import EnginePool
from taifeng.skill.scripts.shell import ShellScriptExecutor
from taifeng.skill.scripts.python import PythonScriptExecutor

pool = await EnginePool.create(
    skills_dir="./skills",
    threads_dir="./threads",
    model_client=client,
    script_executors={
        "shell": ShellScriptExecutor(),
        "python": PythonScriptExecutor(),
    },
)
```

LLM 发起调用：

```json
{
  "tool": "run_script",
  "arguments": {
    "skill_id": "data-prep",
    "script_name": "normalize",
    "args": {"input": "/tmp/raw.csv"}
  }
}
```

### SKILL.md 显式声明（推荐）

```yaml
scripts:
  - name: normalize
    path: scripts/normalize.sh
    language: shell
    timeout_seconds: 30
    description: 把原始 CSV 标准化
    args_schema:
      type: object
      properties:
        input: {type: string}
      required: [input]
```

未声明时 loader 自动扫描 `scripts/*.{sh,py,js,ts}` 隐式发现（默认 60s timeout）。

### 业务自定义 ScriptExecutor

业务侧需 sandbox / 容器 / 远程执行时实现 `ScriptExecutor` 协议：

```python
class FirejailScriptExecutor:
    async def execute(self, inv: ScriptInvocation) -> ScriptResult:
        # 在 firejail / docker run / 内部 sandbox-svc 中执行
        ...

pool = await EnginePool.create(..., script_executors={"shell": FirejailScriptExecutor()})
```

### 关键约束

- **R4 取消**：`inv.cancel` 触发时 SHALL 立即 SIGTERM 整个 process group → 1s grace → SIGKILL
- **R3 可观测**：5 类 EventMsg（`script_execution_started / completed / failed / timeout / killed`）经 `EngineLog` 渠道发出
- **安全默认**：argv 数组 spawn / env 白名单 / stdin DEVNULL / close_fds / per-stream 截断
- **不内置 sandbox**：业务侧通过 `ScriptExecutor` 协议自行 wrap（详见 ADR 0009 §安全模型）

### 完整可运行示例

参见 `examples/basic/skill_with_script.py`：shell + python 两类 executor 共存，端到端跑完三轮 turn。
