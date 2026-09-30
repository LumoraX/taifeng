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
inference:                    # 推理参数（可选，atomic / composite 通用，见下文）
  reasoning_effort: high
  max_output_tokens: 4096

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
结构上不可能有环；引用必须 ∈ `child_skills`（复用白名单 + 环检测）。扩展边界与后路（为何不做任意 DAG / 不做循环原语、
以及唯一保留的加法式切片）见 `docs/architecture/capabilities/skill-orchestration.md` 的「扩展边界与后路」节。

**执行语义（纯编排器）**：声明了 orchestration 的 entry turn **不采样 LLM**，引擎按 `steps` 确定性驱动
子 skill（每个子 skill 内部仍各自走 LLM）。每个 child 收到 `{"input": <entry 种子>}`；**serial / when 段**
额外注入 `{"upstream": [<上一步各 child 输出>]}`（让 summarizer 这类汇总步骤可用），并行组内各 child 互不可见。
`when.condition` 引用的 flag 缺失/非布尔 → emit `orchestration_condition_missing` + turn 硬失败（禁 silent fallback）。

**校验（加载期 fail-fast）**：atomic 声明 orchestration 报错；引用未知 child id 报错；parallel 组内重复报错。
实现见 `src/taifeng/skill/orchestration.py`（解析+校验）+ `src/taifeng/loop/orchestration_exec.py`（执行驱动）。

### inference（推理参数，可选 · atomic / composite 通用）

```yaml
inference:
  reasoning_effort: high      # none | minimal | low | medium | high
  temperature: 0              # [0, 2] 数值
  max_output_tokens: 2048     # >= 1 整数
```

本 skill 作为 turn 的 entry 采样时，已声明的字段写入该次 `ApiRequest`；未声明的为 None，由 provider / 模型默认决定。
顶层 entry 与经 `call_skill` 派发的子 turn 走同一条 `build_api_request` 路径，故父子各按自己的声明下发，互不继承。
atomic 也可声明（它被派发时同样独立采样）；这与 `model` 不同，`model` 仍仅限 composite。

`max_output_tokens` 还参与上下文预算（ADR 0071）：窗口由输入与输出共用，本 turn 生效的输出预留 =
`max(ContextBudget.output_reserve_tokens, max_output_tokens)`，压缩触发、预算提示与发送前 hard 预检都按
「窗口 − 生效预留」算 soft / hard。同样按 turn 的 entry skill 取值——`call_skill` 子 turn 与 detached spawn
子 turn 各用各自的声明，不继承父的放大值。声明值 `>= context_window` 时该 turn 以
`OutputReserveExceedsWindowError` 显式失败（加载期不知道窗口，只能在运行期判定）。详见
[token-accounting-calibration](capabilities/token-accounting-calibration.md)。

**校验（加载期 fail-fast）**：`inference` 非 mapping、含未知键（如拼错的 `temprature`）、`reasoning_effort` 不在枚举内、
`temperature` 非数值或越界、`max_output_tokens` 非正整数（`true` 这类布尔值同样拒绝）→ `SkillValidationError`。
provider 不支持的组合由 provider 显式报错（如 Anthropic 开 extended thinking 时拒绝自定义 temperature），内核不静默丢弃。

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
3. **脚本编排**：在 skill 的 `scripts/` 目录放脚本，经 `run_script` 工具执行（见下文「scripts 与执行器」节）。`scripts:` 非空时 `run_script` **自动并入可见工具集**（`visible_tool_names()`，tool-whitelist 契约），无需也不必手写 `tool_names: [run_script]`——atomic 与 composite 均可用（atomic + scripts 是合法组合）。

> ⚠️ 脚本经 `run_script` 在 **subprocess 隔离**中执行（argv spawn + env 白名单 + stdin DEVNULL），**不能** in-process `import taifeng...` 回调 `call_skill`。需要"脚本里再调子 skill"的确定性流程，请用**声明式编排**而非脚本。

### 严格工具面（strict_tool_names）

默认情况下，composite skill 除 frontmatter 中声明的 `tool_names` 外，还会自动看到内核 skill 工具
`read_skill` 与 `call_skill`；这是多数 ReAct 编排器的默认能力面。若某个 entry 只允许模型调用声明工具，
可在 frontmatter 加：

```yaml
tool_names: [spawn_skill]
strict_tool_names: true
```

此时 `visible_tool_names()` 只返回声明工具与脚本自动派生的 `run_script`，不会再自动加入
`read_skill` / `call_skill`。该开关用于收窄 LLM 的选择面，不替代执行期 `tool-whitelist`
校验；实际派发仍必须命中本轮请求里注入过的工具。

### 加载校验（严格，fail-fast）

loader 对 SKILL.md 的任何问题都在加载期抛 `SkillValidationError`，不跳过、不截断、不强转：

| 情况 | 行为 |
| --- | --- |
| 子目录下没有 `SKILL.md`（如共享素材目录） | 合法跳过 |
| `SKILL.md` 缺 `---` frontmatter / YAML 语法错误 / frontmatter 不是 mapping | 报错 |
| body 超过 `MAX_SKILL_BODY_SIZE`（256KB，UTF-8 字节） | 报错（提示拆到附属文件） |
| `name` / `description` 缺失、为 null 或空串 | 报错 |
| 已知字段类型不符：`entry` 与 `exposure.*_invocable` 须 YAML 布尔；`child_skills` / `tool_names` / `requires.*` 须字符串列表（裸字符串拒绝）；`max_call_depth` 须 >= 1 整数；`model` 须字符串 | 报错 |
| 顶层未知键 | 保留在 `frontmatter_raw` 供业务透传，不报错 |

类型化读取集中在 `skill/frontmatter_fields.py`。多目录加载（`FilesystemSkillRegistry.load([a, b])`）时同名 skill 由靠后的目录覆盖，
覆盖以 warning 日志写明两处路径。热更新（watcher）时新版本校验失败 → 保留旧快照并记异常日志。

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

    # === 推理参数（atomic / composite 通用）===
    inference: SkillInference = field(default_factory=SkillInference)        # reasoning_effort / temperature / max_output_tokens

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
        elif self.type == "composite":
            # composite = 有 agency：child_skills / tool_names / scripts 至少其一非空
            # （ADR 0013 + tool-whitelist：scripts 自动并入 run_script 可见集）
            if not self.child_skills and not self.tool_names and not self.scripts:
                raise SkillValidationError(
                    f"composite skill {self.id!r} 必须至少声明 "
                    "child_skills / tool_names / scripts 之一")
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
    def watch(self) -> AsyncIterator[SkillSnapshot]: ...
```

`watch()` 与 LLM session 的 `stream()` 使用相同异步迭代器契约：协议方法直接返回
`AsyncIterator[SkillSnapshot]`，文件系统实现可用带 `yield` 的 `async def`。消费者
直接 `async for snapshot in registry.watch()`，不先 await；这避免把异步生成器误标成
`Coroutine[..., AsyncIterator[...]]`。

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

## deferred 暴露与 search_skills 发现流（skill-recall）

**召回默认 = inline（工作记忆 / LLM 注意力，`skill_recall=None`）**：默认不注入任何召回后端，全部可见 child 内联进 `<available_child_skills>` 由 LLM 自己找，**不**注册 `search_skills`、**不**启用 deferred。让超量子 skill 走自动 LLM 召回有两条路：① 业务**显式注入**召回后端（`KeywordSkillRecall` / `LlmSkillRecall` / 业务 RAG）；② 开 **opt-in 总闸 `enable_auto_discovery=True`**（默认 False，ADR 0024）——在未显式注入处自动补 `LlmSkillRecall(model_client)` + `LlmSkillVerifier(model_client)`，不改 `None=inline` 零成本默认。启用后当 caller 可见 child 数膨胀到「装不进一次 prompt」（auto 模式超 `recall_threshold`）即 **deferred 暴露**：给 LLM 一个 `search_skills(query)` 工具按需召回 top_k 候选，**召回后再经验证门据完整 body 判输入要求适配**滤掉误召，再据返回 `call_skill` 派发。海量 child 但未注入 / 未开闸时仍走 inline（prompt 会变大——这是「默认 LLM 自己找」的应有之义，超大规模自行选注入 / 开闸）。

- **inline / deferred 单一真相**：`skill.visibility.effective_child_recall(entry, child_count, threshold, has_recall_backend)` 是 system prompt 文本构建与 per-turn 工具裁剪的**唯一判定**。`child_recall: inline` 强制内联；`child_recall: deferred` 显式要召回——有后端则 deferred，**无后端抛 `SkillValidationError`**（禁 silent 降级 inline）；`auto`（默认）时**有后端**且「G4 过滤后可见 child 数 `> recall_threshold`（构造参数，默认 50）」才切 deferred，**无后端恒 inline**。两侧同判定，保证「prompt 是否列 child」与「是否暴露 search_skills」严格一致。
- **召回作用域 = 白名单内 G4 过滤**：召回语料池由 `skill.visibility.visible_child_skills` 构建——它是 inline 列表那套 G4 过滤（G4b `model_invocable=False` 隐藏 / G4a `requires` 不满足剔除）的**唯一实现**，故 deferred 召回拿不到本应隐藏的 child（非 G4 旁路）。池**仅含 caller 的 `child_skills`**（白名单封闭由内核钉死，召回后端只在 pool 内排名）。
- **召回后端阶梯（默认 inline，可选注入）**：`SkillRecall` 协议把后端按「离工作记忆远近 = 成本」排成阶梯——① inline（默认，零调用）；② `KeywordSkillRecall`（可选注入，零依赖 BM25-lite，确定性）；③ `LlmSkillRecall`（可选注入或总闸自动补，一次性子 LLM 调用语义挑选，**非确定性**，pool 须能放进一次 prompt）；④ 向量 / RAG（业务注入，ADR 0017③）。`recall_default_top_k`(5) / `recall_max_top_k`(20) 是 `search_skills` 的 top_k 默认与上界（构造参数，仅启用召回时生效）。
- **召回后验证门（适配精验）**：召回只看 `description`（长相，浅），启用验证（注入 `skill_verifier` 或开总闸）时再经 `SkillVerifier` 拉**完整 SKILL.md body**，LLM 判「就当前任务能提供的输入 / 条件，该能力声明要的输入是否满足、前提是否具备」（判**适配**，不判能否跑通），滤掉「描述像但输入要求不满足」的误召。`LlmSkillVerifier` 有 C2 护栏（只验前 `verify_max_candidates`(5) 个、单 body 超 `verify_body_char_limit`(4000) 截断）；`VerifiedCandidate` 把 `recall_confidence`（长相）与 `verify_confidence`（适配）**分字段**（防呆）。
- **置信路由（禁 silent）**：启用验证时 `search_skills` 走「召回 → 验证 → 路由」——有 applicable 候选透 `[{skill_id, description, confidence(=verify), reason}]`；全不适用 / 召回空返回**显式** `{"no_match": true, "hint": ...}`，不返回空数组伪装。未启用验证时退化为 0023 行为（召回直接路由，`confidence` 为召回长相、含 `matched_snippet`，内核**不据其分流**）。
- **选择溯源连回战绩**：经 `search_skills` 召回 / 验证选中再 `call_skill` 派发的 skill，其 `SkillExecutionRecord.selection_origin="discovered"` + `selection_confidence`（= payload `confidence`：启用验证时即 `verify_confidence`，否则即召回长相；复用 v1 [skill-outcome-record](capabilities/skill-outcome-record.md) 的 `SelectionOrigin` Literal）；未经召回的派发仍为 `whitelist` / `None`。
- **按置信度分流（opt-in，[skill-selection-gate](capabilities/skill-selection-gate.md)）**：注入 `selection_gate=SkillSelectionGate(policy, trial_judge)` 后，`search_skills` 给每个候选标 `route`（`proceed` / `trial` / `escalate`，全部 `escalate` 时返回带 `low_confidence` 的 `no_match`），`call_skill` / `spawn_skill` 在 `DispatchPolicy.check` 之后过分流门：`trial` 档须模型先 `read_skill` 或试用门放行，`escalate` 档本轮不可派发。只约束本轮经召回看到的 skill；判定全部由 history 推导，不持有状态。事件 `skill_selection_routed` / `skill_selection_gated`。
- **白名单外授权（opt-in，[skill-authorization](capabilities/skill-authorization.md)）**：`DispatchPolicy(authorization=...)` 注入 `SkillAuthorizationPolicy` 后，召回池并入白名单外可发现的 skill（结果里标 `requires_authorization`），即便 child 列表是 inline 也暴露 `search_skills`；`call_skill` 的目标不在白名单时先过 `authorize`，放行只豁免白名单一层，深度 / 环 / entry、分流门、hook、`skill_dispatch` 审批照常。参考实现：`CallbackSkillAuthorization`（业务回调）、`PermissionSkillAuthorization`（权限门的规则 / 可复用授权 / 人工审批，范围 `skill_authorization`）。`spawn_skill` 与声明式编排不走白名单外授权。
- **可观测**：`skill_search_invoked`（query / top_k / pool_size）+ `skill_candidates_returned`（count / top_ids）覆盖召回链；启用验证时追加 `skill_candidates_verified`（verified_count / dropped_count）覆盖验证门。三事件均不进 LLM 视图。

完整数据契约与场景见 `capabilities/skill-recall.md`；为何这么定见 ADR 0023（召回）+ ADR 0024（opt-in 总闸 + 验证门）。

## 按战绩算分、影子评估与生效（skill-working-set）

每次 `call_skill` 子 skill 到达终态都会产生一条战绩（`skill_outcome_recorded`）。`SkillFitnessStore` 把它们聚合成
每个 skill 的成败计数与成本累计；`working_set` 模块在聚合之上算分并规划工作集：

```
skill_outcome_recorded ──► SkillFitnessShadow（TelemetrySink，attach 到 engine）
                              ├─ store.record                      聚合（按 call_id 幂等）
                              ├─ FitnessScorer.score × 全部 skill  Wilson 置信下界，可选成本折减
                              ├─ plan_working_set                  提拔 / 逐出 / 隔离 / 解除（无状态重算）
                              └─ ShadowObserver.on_evaluation      只记录「如果生效会发生什么」
```

- **长相与战绩分离**：算分只读成败计数与成本，不读 `selection_confidence`。
- **放弃不算失败**；没有成败样本的 skill 得 0 分；样本越少分数被压得越低。
- **超预算逐出最低分者**，不是最早进入者；**高选中、低成功**的 skill 被隔离且不得提拔。
- **影子模式**：`SkillFitnessShadow` 经事件流旁路挂接，prompt 组装、召回、派发路径都不持有它的引用，
  skill 的可见性与排序不受影响。
- **生效模式**：`DispatchPolicy(working_set=SkillWorkingSet(...), trust=...)`。子 skill 到达终态后战绩交给
  `observe`，变更打成 `skill_promoted` / `skill_evicted` / `skill_quarantined` / `skill_released`；
  每个 turn 首次采样前取一份结论快照，整 turn 使用：

  ```
  快照.promoted ──► deferred 模式的 system prompt 直接列出这些 child（免搜索）
  快照.hidden   ──► child 列表、召回池、白名单外可发现范围都不含这些 skill
  blocks(id)    ──► call_skill 拒绝派发（仅 quarantine_effect="block"）
  ```

- **来源信任分层**：`SkillTrustPolicy` 给出 `trusted` / `standard` / `untrusted`，默认实现
  `SourceTrustPolicy` 按加载来源分层（`FilesystemSkillRegistry(..., sources={目录: 来源})`）。层级写进战绩记录的
  `trust_tier`、召回候选与白名单外授权请求；`WorkingSetPolicy.tier_rules` 按层级调整提拔与隔离的门槛；
  `ThresholdSelectionPolicy(trial_tiers=...)` 让指定层级的候选置信再高也须先试用。层级不改战绩分。

数据契约见 `capabilities/skill-working-set.md`；为何这么定见 ADR 0077（算分与影子）、0090（生效与信任分层）。

## scripts 与执行器（scripts-runtime）

SKILL.md 中 `scripts:` 字段声明的脚本不是装饰品 —— 由 `run_script` 内置工具暴露给 LLM 执行（`scripts:` 非空即自动进可见集，见 `capabilities/tool-whitelist.md`）。详见 ADR 0009 / `capabilities/script-execution.md`。

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

渐进加载第三层：`read_skill(skill_id, path)` 读取该 skill 目录内的附属文件（正文里引用的 `references/api.md`、
`FORMS.md` 等），让作者把低频细节移出正文。约束：`path` 必须是相对路径且解析后（含符号链接展开）落在 skill 目录内，
否则 `not_visible`；须为 UTF-8 文本（否则 `not_text`），≤ `MAX_SKILL_FILE_BYTES`（256KiB，否则 `too_large`）；
可见性与读正文同一规则。文件读取在工作线程执行。frontmatter 的 `allowed-tools`（Agent Skills 生态字段）不映射为
`tool_names`：前者是「免审批可用」，后者是「可见白名单」，且工具名不通用；它保留在 `frontmatter_raw` 供宿主映射到权限策略。

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
