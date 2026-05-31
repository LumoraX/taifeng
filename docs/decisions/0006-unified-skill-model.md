# ADR 0006: 统一 Skill 模型 —— 删除 Agent 概念

- 状态：Accepted
- 日期：2026-05-23
- Supersedes：ADR 中提及的 `AgentDefinition` / `AgentInstance` 抽象（未正式立项，仅在对话中讨论过）

## 背景

前两轮设计讨论中曾提议引入 `AgentDefinition`（角色 / 领域专家定义）与 `SkillDefinition`（原子能力）的二元模型。例如：

- Agent = 「程序员」/「数据分析师」（绑定一组 skill + 模型 + 工具）
- Skill = 「代码审查」/「SQL 查询」（原子能力）

进一步讨论后意识到：**Agent 这个概念并没有真正消除分层**，只是把"高层 skill"换了个名字。命名分裂导致：
- 维护两套 markdown 格式（AGENT.md + SKILL.md）
- 两套 loader / registry / 权限表
- 文档体系翻倍

## 决策

**统一为 Skill 一个核心抽象，通过子类型字段区分原子 vs 组合**。

```yaml
# SKILL.md frontmatter
type: atomic | composite       # 原子 / 组合
entry: true | false             # 是否可作为会话入口
child_skills: [...]             # composite 可调用的子 skill 白名单
max_call_depth: 6               # 递归深度上限
```

## 三个边界确认

### 1. 调用语义：M1 支持 LLM 编排 + 工具编排，流程编排 DAG 推迟

| 编排方式 | 谁决定调谁 | M0–M5 支持 |
| --- | --- | --- |
| **LLM 编排** | 主 LLM 读 composite body 后自主调 `call_skill(id, args)` tool | ✅ M3 |
| **工具编排** | composite skill 的 `scripts/` 内代码显式 `await call_skill(...)` | ✅ M3 |
| **流程编排 (DAG)** | frontmatter 声明静态依赖图 | ❌ 推迟到 M6+ |

引擎层只暴露一个 `call_skill` tool 与一个 Python `call_skill()` 入口；具体怎么用由 skill 自己定义。

### 2. 入口标记：`entry: true` 才能作为会话入口

- Atomic skill 通常 `entry: false`（默认）—— 没有角色感，不适合直接对话
- Composite skill 视产品定义决定 `entry: true/false`
- 业务层启动 session 时必须传 `entry_skill_id`，引擎校验 `entry == true` 才放行

### 3. 订阅控制入口（模式 1）

- 业务表 `tenant_entry_skills` 一维数组存「该租户可见的入口 skill」
- 入口 skill 调用子 skill 时**不再校验订阅**，由 entry skill 的 `child_skills` 静态声明背书
- 运营做产品包时，**把 entry skill 及其可达子图视为一个产品打包售卖**
- 不实现按调用计费 / 子 skill 单独售卖

## 递归与环检测

### 递归深度
- 默认 `max_call_depth = 6`
- Composite 可嵌 composite 嵌 composite（无层数限制，仅深度限制）
- 业务侧可在 `TaifengPool` 全局覆盖默认值；个别 entry skill 可在 frontmatter 覆盖

### 环检测必须两层

**静态环检测（load-time）**：
- `SkillRegistry.discover()` 完成后基于 `child_skills` 声明构建有向图
- 用 DFS / Tarjan SCC 检测环
- 任一环存在 → **加载失败**，进程拒绝启动
- 拦截"开发者声明错误"（写错 child_skills 把自己引回来）

**动态环检测（runtime）**：
- 每次 `call_skill` 调用 push 调用栈，包含完整路径
- 派发前检查：目标 skill 是否已在当前调用栈 → 是则**拒绝**
- 返回错误：`"circular_call: a → b → c → a"`
- 拦截"LLM 调用错误"（静态图无环但 LLM 通过不同路径绕回原点）

两层缺一不可：
- 只有静态：LLM 编排时可能动态构造环
- 只有动态：开发者错误要到运行时才暴露，浪费集成测试时间

## 数据结构

```python
@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    body: str
    
    # === 分层标记 ===
    type: Literal["atomic", "composite"]
    entry: bool = False
    
    # === Composite 特有字段（atomic 全部留空）===
    child_skills: frozenset[str] = frozenset()
    tool_names: frozenset[str] = frozenset()
    max_call_depth: int = 6
    model: str | None = None              # entry skill 模型偏好


@dataclass(frozen=True)
class CallFrame:
    skill_id: str
    call_id: str
    started_at: datetime
    parent_call_id: str | None


@dataclass(frozen=True)
class CallStack:
    frames: tuple[CallFrame, ...] = ()
    
    @property
    def depth(self) -> int:
        return len(self.frames)
    
    def contains(self, skill_id: str) -> bool:
        return any(f.skill_id == skill_id for f in self.frames)
    
    def path(self) -> list[str]:
        return [f.skill_id for f in self.frames]
    
    def push(self, skill_id: str, call_id: str) -> "CallStack":
        parent = self.frames[-1].call_id if self.frames else None
        frame = CallFrame(skill_id=skill_id, call_id=call_id,
                          started_at=datetime.utcnow(), parent_call_id=parent)
        return CallStack(frames=self.frames + (frame,))


class DispatchPolicy:
    """派发策略 —— 深度与环检测的决策点。"""
    
    def check(
        self,
        stack: CallStack,
        target: SkillDefinition,
    ) -> "DispatchVerdict":
        if stack.depth >= target.max_call_depth:
            return DispatchVerdict.reject(
                reason="max_depth_exceeded",
                path=stack.path(),
            )
        if stack.contains(target.id):
            return DispatchVerdict.reject(
                reason="cycle_detected",
                path=stack.path() + [target.id],
            )
        if target.id not in stack.frames[-1].caller.child_skills:
            return DispatchVerdict.reject(
                reason="not_in_child_skills_whitelist",
            )
        return DispatchVerdict.allow()
```

## 后果

### 正面
- 一套 markdown / loader / registry / 权限表
- 文档体系减半
- 业务表 schema 简化为 `tenant_entry_skills` 一张
- 与"Unix philosophy" 对齐：everything is a skill
- Prompt cache 边界清晰：每个 entry skill 的 system prompt 静态，同 entry skill 所有用户 cache 共享

### 负面
- 词汇与 Claude Code「subagent」/ codex「AGENTS.md」生态有距离，对外讲解需要解释"我们叫 entry skill"
- composite skill 的 frontmatter 字段多于 atomic（5 个独有字段）—— 容忍这个不对称
- 业务上若需"按调用计费"必须未来扩展（M6+）

### 缓解措施
- 文档对外讲解时给出术语映射表：「Taifeng entry skill ≈ Claude Code subagent」
- 提供 `taifeng skill validate` CLI 校验 frontmatter 完整性 + 静态环检测
- 计费扩展点预留在 `TelemetrySink` 接口，未来按 `call_skill` 调用次数打点即可

## 与 ADR 0003 的关系

ADR 0003「Skill = 上下文，不是 Tool」**不变**。本 ADR 在其基础上：
- 把 skill 分为 atomic / composite 两个子类型
- composite 通过 `call_skill` tool 实现 skill-to-skill 调用
- 仍然遵守"skill body 不全量塞 system prompt"的红线

## 相关

- [架构：Skill 系统](../architecture/skill-system.md)（已按本 ADR 重写）
- [架构：总览](../architecture/overview.md)（§7 模块切分更新，删除 `agent/` 包）
- ADR 0003（skill-as-context 范式）
