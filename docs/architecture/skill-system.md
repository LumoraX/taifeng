# Skill 系统（统一模型）

> §1.1 —— SKILL.md 文档化技能、原子 / 组合两态、entry 入口、call_skill 递归调用。
>
> ⚠️ 本文档遵循 ADR 0006 的统一 Skill 模型。**没有 Agent 概念**。

## 设计目标

- **唯一抽象**：所有能力单元都是 `Skill`，没有 Agent / Skill 二元对立
- **两态分层**：通过 `type: atomic | composite` 字段区分原子 / 组合
- **入口锁定**：只有 `entry: true` 的 skill 能作为会话入口
- **递归调用**：composite skill 可通过 `call_skill` 调子 skill，深度可配，强制环检测

## SKILL.md 格式

### Composite Skill（组合能力 / 角色入口）

```markdown
---
name: code-reviewer
display_name: 代码审查专家
description: 多维度代码审查 —— 协调子 skill 完成风格 / 安全 / 性能审查
version: 1.0.0

# 分层标记
type: composite
entry: true                   # 可作为会话入口
model: claude-opus-4-7        # entry skill 偏好模型（业务层可覆盖）

# Composite 特有字段
child_skills:                 # 静态白名单：本 skill 能调用的子 skill
  - style-checker
  - security-scanner
  - perf-analyzer
  - test-suggester
tool_names: [file_read, http_request]
max_call_depth: 6             # 递归深度上限
---

# 代码审查专家

你是一位资深代码审查工程师。

## 工作范围
- 阅读 PR diff
- 派发多维度审查（风格 / 安全 / 性能 / 测试）
- 汇总并给出可执行修改建议

## 子能力调用

当需要专门维度时，调用以下子 skill：

- `call_skill("style-checker", {...})` —— 代码风格审查
- `call_skill("security-scanner", {...})` —— 安全漏洞扫描
- `call_skill("perf-analyzer", {...})` —— 性能瓶颈分析

## 工作原则
- 引用问题必须包含文件路径、行号、严重性
- ...
```

### orchestration（声明式编排，可选 · 仅 composite）

composite skill 可在 frontmatter 声明子步骤的「并行 / 顺序 / 条件」编排骨架。**不声明则完全回退**到
LLM 读 body 自主决策 + 隐式并发（零行为变更）。三原语：

```yaml
orchestration:
  steps:                              # 有序列表：段间天然串行（barrier），段内表达并发
    - parallel: [route-a, route-b]    # 并行组：同批并发派发（复用 dispatch_batch + max_parallel_tool_calls）
    - serial: [summarizer]            # 顺序段：强制 Semaphore(1)，即便全局并发上限很大
    - when:                           # 单层条件（嵌套深度限 1 层，then/else 内不可再嵌 when）
        condition: needs_weather      # 上一步 child 结构化输出里的布尔 flag 名
        then: [weather, traffic]      # 裸列表=并行；亦支持 {serial: [...]} / {parallel: [...]}
        else: {serial: [fallback]}    # 可选；省略则 condition=false 时跳过本段
```

**结构**：线性 fork-join（series-parallel，DAG 的确定性子集），非任意依赖图。顺序由列表位置定义，
结构上不可能有环；引用必须 ∈ `child_skills`（复用白名单 + 环检测）。升级到真 DAG 的后路见
`docs/architecture/capabilities/skill-orchestration.md` §7（加法式扩展，不破坏 `steps` 写法）。

**执行语义（纯编排器）**：声明了 orchestration 的 entry turn **不采样 LLM**，引擎按 `steps` 确定性驱动
子 skill（每个子 skill 内部仍各自走 LLM）。每个 child 收到 `{"input": <entry 种子>}`；**serial / when 段**
额外注入 `{"upstream": [<上一步各 child 输出>]}`（让 summarizer 这类汇总步骤可用），并行组内各 child 互不可见。
`when.condition` 引用的 flag 缺失/非布尔 → emit `orchestration_condition_missing` + turn 硬失败（禁 silent fallback）。

**校验（加载期 fail-fast）**：atomic 声明 orchestration 报错；引用未知 child id 报错；parallel 组内重复报错。
实现见 `src/taifeng/skill/orchestration.py`（解析+校验）+ `src/taifeng/loop/orchestration_exec.py`（执行驱动）。

### Atomic Skill（原子能力）

```markdown
---
name: style-checker
display_name: 风格审查
description: 检查代码风格（命名 / 缩进 / 注释规范）
version: 1.0.0

type: atomic
# entry / child_skills / tool_names 全部省略；atomic skill 不可作为入口也不可调子 skill
---

# 风格审查

按 PEP 8 / Google Style 等规范审查 diff，列出违规处...
```

### 三种编排并存

skill 设计者可在三种编排间自由选择：

1. **LLM 编排**：在 composite 的 skill body 内由 LLM 读 body 自主决策 `call_skill`（默认；不声明 orchestration 时走这条）。
2. **声明式编排（B）**：在 frontmatter 声明 `orchestration` 块（见上文「orchestration」节），引擎按 `steps` 确定性驱动、**不采样 LLM**。适合固定的 fork-join 流程。
3. **脚本编排**：在 composite 的 `scripts/` 目录放脚本，经 `run_script` 工具执行（见下文「scripts 与执行器」节）。

> ⚠️ 脚本经 `run_script` 在 **subprocess 隔离**中执行（argv spawn + env 白名单 + stdin DEVNULL），**不能** in-process `import taifeng...` 回调 `call_skill`。需要"脚本里再调子 skill"的确定性流程，请用**声明式编排**而非脚本。

## 核心抽象

```python
# src/taifeng/skill/definition.py

from dataclasses import dataclass, field
from typing import Literal

@dataclass(frozen=True)
class SkillDefinition:
    """统一 skill 描述符。原子 / 组合通过 type 字段区分。"""

    # === 通用字段 ===
    id: str                                # 目录名，全局唯一
    name: str
    description: str
    version: str
    body: str                              # markdown 正文
    body_path: Path

    # === 分层标记 ===
    type: Literal["atomic", "composite"]
    entry: bool = False                    # 是否可作为会话入口

    # === Composite 特有字段（atomic 必须全部留空）===
    child_skills: frozenset[str] = frozenset()
    tool_names: frozenset[str] = frozenset()
    max_call_depth: int = 6
    model: str | None = None               # entry skill 偏好模型

    # === 声明式编排（B，仅 composite 可声明；atomic 声明即报错）===
    orchestration: OrchestrationSpec | None = None

    # === G4 可见性治理（atomic / composite 通用）===
    requires: SkillRequirements = field(default_factory=SkillRequirements)   # bins/env/os 资格门控
    exposure: SkillExposure = field(default_factory=SkillExposure)           # model_invocable / user_invocable

    # === 业务透传 ===
    frontmatter_raw: dict = field(default_factory=dict)
    scripts: tuple[ScriptDescriptor, ...] = ()
    source: SkillSource = "user"           # system | user | marketplace

    def validate(self) -> None:
        """启动期约束校验。失败立即抛 SkillValidationError（不是 assert）。"""
        if self.type == "atomic":
            # atomic 不可声明 child_skills / tool_names，也不可作为 entry（无豁免位）
            if self.child_skills:
                raise SkillValidationError(f"atomic skill {self.id!r} 不能声明 child_skills")
            if self.tool_names:
                raise SkillValidationError(f"atomic skill {self.id!r} 不能声明 tool_names")
            if self.entry:
                raise SkillValidationError(f"atomic skill {self.id!r} 默认不可作为 entry")
```

```python
# src/taifeng/skill/registry.py

@dataclass(frozen=True)
class SkillSnapshot:
    """注册表不可变快照。"""
    version: int
    skills: tuple[SkillDefinition, ...]
    # 派发预计算：composite 的可达子图
    reachable_graph: dict[str, frozenset[str]] = field(default_factory=dict)

    def get(self, skill_id: str) -> SkillDefinition | None: ...

    def entries(self) -> tuple[SkillDefinition, ...]:
        """所有 entry=true 的 skill。"""
        return tuple(s for s in self.skills if s.entry)


class SkillRegistry(Protocol):
    async def discover(self) -> SkillSnapshot:
        """全量扫描 + 静态环检测。任一环存在则抛 CircularSkillReference。"""

    def get(self, skill_id: str) -> SkillDefinition | None: ...
    def snapshot(self) -> SkillSnapshot: ...
```

## 静态环检测（load-time）

```python
def detect_cycles(skills: dict[str, SkillDefinition]) -> list[list[str]]:
    """Tarjan SCC 算法，返回所有强连通分量（长度 > 1 即环）。

    Returns:
        list of cycle paths, 每个 path 是循环上的 skill_id 序列
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {sid: WHITE for sid in skills}
    cycles: list[list[str]] = []
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for child_id in skills[node].child_skills:
            if child_id not in skills:
                continue                          # 引用未知 skill，加载阶段已警告
            if color[child_id] == GRAY:
                # 环：从 stack 中找到 child_id 起点
                start = stack.index(child_id)
                cycles.append(stack[start:] + [child_id])
            elif color[child_id] == WHITE:
                dfs(child_id)
        stack.pop()
        color[node] = BLACK

    for sid in skills:
        if color[sid] == WHITE:
            dfs(sid)
    return cycles


class CircularSkillReference(Exception):
    """启动期发现 skill 调用环 —— 拒绝启动。"""
```

`SkillRegistry.discover()` 流程：

```python
async def discover(self) -> SkillSnapshot:
    skills = await self._scan_and_parse()        # 解析所有 SKILL.md

    # 1. 单个 skill 自校验
    for s in skills.values():
        s.validate()

    # 2. child_skills 引用完整性
    for s in skills.values():
        unknown = s.child_skills - set(skills)
        if unknown:
            raise UnknownChildSkill(f"{s.id} → {unknown}")

    # 3. 静态环检测
    cycles = detect_cycles(skills)
    if cycles:
        msg = "\n".join(" → ".join(p) for p in cycles)
        raise CircularSkillReference(
            f"Detected {len(cycles)} cycle(s) in skill graph:\n{msg}"
        )

    # 4. 可达子图预计算（业务订阅校验用）
    reachable = compute_reachable_graph(skills)

    return SkillSnapshot(
        version=self._next_version(),
        skills=tuple(skills.values()),
        reachable_graph=reachable,
    )
```

## 动态环检测（runtime）

```python
# src/taifeng/skill/dispatch.py

@dataclass(frozen=True)
class DispatchVerdict:
    allowed: bool
    reason: str | None = None
    path: list[str] = field(default_factory=list)

    @classmethod
    def allow(cls) -> "DispatchVerdict":
        return cls(allowed=True)

    @classmethod
    def reject(cls, reason: str, path: list[str] | None = None) -> "DispatchVerdict":
        return cls(allowed=False, reason=reason, path=path or [])


class DispatchPolicy:
    """每次 call_skill 派发前的策略检查。"""

    def check(
        self,
        stack: CallStack,
        caller: SkillDefinition,
        target: SkillDefinition,
    ) -> DispatchVerdict:
        # 1. 深度限制（caller 的 max_call_depth 决定调用图深度上限）
        max_depth = caller.max_call_depth
        if stack.depth >= max_depth:
            return DispatchVerdict.reject("max_depth_exceeded", stack.path())

        # 2. 动态环检测
        if stack.contains(target.id):
            return DispatchVerdict.reject(
                "cycle_detected",
                stack.path() + [target.id],
            )

        # 3. 白名单校验
        if target.id not in caller.child_skills:
            return DispatchVerdict.reject(
                "not_in_whitelist",
                [caller.id, target.id],
            )

        # 4. 不能调 entry skill（entry skill 是会话起点，不该被嵌套）
        if target.entry:
            return DispatchVerdict.reject(
                "cannot_call_entry_skill",
                [caller.id, target.id],
            )

        return DispatchVerdict.allow()
```

## call_skill Tool（LLM 编排接口）

```python
class CallSkillTool:
    """暴露给 LLM 的 skill 调用工具。

    LLM 视角：
        tool: call_skill
        args: { "skill_id": str, "args": dict }
    """

    name = "call_skill"
    parallel_safe = False                  # skill 调用涉及 LLM 子调用，独占

    async def execute(
        self,
        args: dict,
        ctx: ToolContext,
    ) -> ToolResult:
        target = ctx.snapshot.get(args["skill_id"])
        if target is None:
            return ToolResult.error("unknown_skill")

        verdict = ctx.dispatch_policy.check(
            stack=ctx.call_stack,
            caller=ctx.current_skill,
            target=target,
        )
        if not verdict.allowed:
            return ToolResult.error(verdict.reason, data={"path": verdict.path})

        # 派发子 turn（共享父会话的 store / model_client）
        sub_result = await ctx.dispatcher.run_sub_skill(
            target=target,
            args=args["args"],
            parent_stack=ctx.call_stack,
            cancel=ctx.cancel.child(f"skill:{target.id}"),
        )

        return ToolResult.ok(sub_result.output)
```

## scripts 与执行器（scripts-runtime）

SKILL.md 中 `scripts:` 字段声明的脚本不是装饰品 —— 由 `run_script` 内置工具暴露给 LLM 执行。详见 ADR 0009 / `capabilities/script-execution.md`。

### 数据流

```
LLM → run_script(skill_id, script_name, args)
  ├─ 1. skill 查找
  ├─ 2. script_name ∈ skill.scripts 查找
  ├─ 3. args_schema 校验
  ├─ 4. executor = script_executors[descriptor.language]
  ├─ 5. pre_script_use hook 链（支持 args_override）
  ├─ 6. PermissionPolicy.check(scope='script_exec', target='<skill>/<script>')
  ├─ 7. executor.execute(invocation) → ScriptResult
  ├─ 8. post_script_use hook（仅审计；hook 异常不影响 ToolResult）
  └─ 9. 打包 ToolResult + 5 类 EventMsg
```

### SKILL.md 中声明

```yaml
scripts:
  - name: normalize
    path: scripts/normalize.sh
    language: shell    # shell | python | custom
    timeout_seconds: 30
    description: 把 CSV 标准化（给 LLM 看的）
    args_schema:
      type: object
      properties:
        input_path: {type: string}
      required: [input_path]
```

未显式声明时 loader 自动隐式发现 `scripts/*.{sh,py,js,ts}`（默认 timeout=60s / args_schema={}）。`path` 必须落在 skill 目录下（防越权）。

### 业务侧注入 executor

```python
from taifeng.skill.scripts.shell import ShellScriptExecutor
from taifeng.skill.scripts.python import PythonScriptExecutor

pool = EnginePool.create(
    ...,
    script_executors={
        "shell": ShellScriptExecutor(),
        "python": PythonScriptExecutor(),
        # 自定义 ScriptExecutor 协议实现 —— 容器 / 沙箱 / 远程 RPC
        "custom": YourFirejailExecutor(),
    },
)
```

未注入对应 language → `run_script` 返回 `no_executor_for_language`。

### Subprocess 隔离（src 默认实现）

| 控制 | 做法 |
| --- | --- |
| argv 数组 spawn | 防 shell injection（`"; rm -rf /"` 不被解析） |
| env 白名单 | 仅 `PATH / HOME / LANG / LC_ALL`；secret 不泄漏 |
| stdin DEVNULL | 防 LLM 把对话内容注入子进程 |
| process group kill | timeout / cancel 时 grandchild（如 `sleep`）一起 SIGTERM → SIGKILL |
| per-stream 截断 | stdout / stderr 各 `max_output_bytes`；超限 `truncated=True` |
| close_fds | 子进程仅可见 stdout/stderr/stdin |

### 与 shell_exec 工具的区别

- `shell_exec`：通用 shell 入口，PermissionPolicy 按命令字符串匹配
- `run_script`：限定到 skill 内声明的 script，粒度更细 + args_schema 校验 + hook 闭环

生产环境建议 `shell_exec` 默认 deny、所有 shell 行为收口到 `run_script`。

参见示例：`examples/basic/skill_with_script.py`。

## 注入策略（system prompt）

只有**入口 skill 的 body**进 system prompt（带 `<entry_skill>` XML 块）；子 skill 列表通过 `<available_child_skills>` 块注入名称 + 描述（不带 body）；子 skill 的 body 由 LLM 调 `read_skill(id)` 按需读。

```xml
<entry_skill id="code-reviewer">
你是一位资深代码审查工程师。
... (body)
</entry_skill>

<available_child_skills>
You can invoke these skills via `call_skill(skill_id, args)`:

- style-checker: 代码风格审查（PEP 8 / Google Style 等）
- security-scanner: 安全漏洞扫描（SQL 注入 / XSS / 密钥泄露）
- perf-analyzer: 性能瓶颈分析
- test-suggester: 测试覆盖建议
</available_child_skills>
```

## 业务层订阅模型

```python
# 业务表（业务侧持久化，不进引擎）
class TenantSubscription:
    tenant_id: str
    allowed_entry_skills: set[str]         # 仅入口；子 skill 由 entry 的 child_skills 背书


async def check_session_authorization(
    tenant_id: str,
    entry_skill_id: str,
) -> None:
    sub = await tenant_repo.get_subscription(tenant_id)
    if entry_skill_id not in sub.allowed_entry_skills:
        raise PermissionDenied(f"entry skill {entry_skill_id} not subscribed")
```

订阅校验只发生在**会话开启**那一刻；进入会话后 `call_skill` 派发只校验 child_skills 白名单，**不再回查订阅表**。

## 实现来源

加载器范式孵化自既有 SKILL.md 加载实现（frontmatter 解析、文件 watcher、子进程执行），在此基础上补齐了
Taifeng 特有能力：`type` / `entry` / `child_skills` / `max_call_depth` 字段解析、静态环检测（`detect_cycles`）、
派发预计算（`reachable_graph`）、registry 协议化（业务侧自行实现 `SkillRegistry`，无 DB 强耦合）。以上均已落地。

## 测试用例（M2 验收）

> 全部已覆盖（`tests/test_dispatch.py` / `test_skill.py` / `test_orchestration.py` / `test_skill_visibility.py` / `test_script_*.py`）。

- [x] 加载含环的 skill 集合 → 启动失败，错误信息包含完整环路径
- [x] 加载引用未知子 skill 的 composite → 启动失败
- [x] LLM 调用未声明的子 skill → `call_skill` 返回 `not_in_whitelist`
- [x] LLM 通过不同路径绕回当前 skill → `cycle_detected`
- [x] 派发深度达 `max_call_depth` → `max_depth_exceeded`
- [x] LLM 调用另一 entry skill → `cannot_call_entry_skill`
- [x] 子 skill 的 body 不进 system prompt；LLM 主动 `read_skill(id)` 才取
- [x] 声明式编排：composite 的 `orchestration` 块经加载期 fail-fast 校验 + 确定性执行（`test_orchestration.py`）
