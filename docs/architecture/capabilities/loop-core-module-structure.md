# loop 核心模块结构（engine / turn）

> 能力契约。决策见 [ADR 0038](../../decisions/0038-loop-core-module-split.md)；
> spawn 子系统的同族契约见 `openspec/specs/spawn-module-structure`。

## 数据契约

### 模块边界

`loop/engine.py` 与 `loop/turn.py` 按职责切分为协作者模块。**抽出的每个模块 ≤ 800 行**。

| 宿主 | 模块 | 形态 | 职责 |
| --- | --- | --- | --- |
| engine | `engine_types` | 类型 | `DeliveredEvent` / `_Subscriber` / `_PendingTurn` / `_TERMINAL_KINDS` |
| engine | `engine_events` | 协作者 | emit / 终态记账 / 投递与丢弃 / 高低水位告警 |
| engine | `engine_operations` | 协作者 | operation 派发 / 守护 / 终结事件 / 遗忘 / 收敛 |
| engine | `engine_lifecycle` | 协作者 | memory 会话结束 / 生命周期收敛 / 孤儿 submission 终结 |
| engine | `suspension_ttl` | 协作者 | 挂起到期武装 / 触发 / 路由裁决 / 冷重武装 |
| engine | `engine_gate` | 协作者 | 根闸获取释放 / 受闸 op / turn 执行 / 会话 token 天花板 / 挂起闸 |
| engine | `engine_runner` | 协作者 | 残留注入排空 / runner 回写 / 构建并跑 / post-turn 钩子 |
| engine | `engine_resume` | 协作者 | 根 thread Resume（配对核销 / 补 gap / 续采样） |
| engine | `child_resume_chain` | 协作者 | **call_skill 子链**续跑（逐层回填父 `function_call_output`） |
| engine | `suspension_access` | 协作者 | thread 逻辑历史读取 / 活跃挂起定位 / 核销副作用 / 已批准工具执行 / 手动压缩 |
| engine | `engine_ops` | 模块函数 | rewind / rollback / update_budget / update_instructions / refresh_snapshot |
| turn | `turn_helpers` | 模块函数 | 摘要哈希 / 孤儿 call_id / 末条用户文本 / 失败上下文 / Responses 采样项 |
| turn | `turn_guards` | 协作者 | 延迟暴露判定 / SYSTEM_RETRY 挂起判定 / 资源守卫触顶转挂起 |
| turn | `turn_context` | 协作者 | 记忆预取回写 / pre-evict 抢救 / pinned state 重注 / 预算提示 |
| turn | `turn_sample` | 协作者 | prompt 指纹 / 结构性中断判定 / 三段采样 |
| turn | `turn_tooling` | 协作者 | 工具 outcome 记账 / 选择追踪 / doom-loop 提示 / ToolContext 构造 / seed 补全 |
| turn | `turn_persist` | 协作者 | 挂起落盘 / 会话上限判定 / usage 累加 / 半程 assistant 落史 / 注入排空 |
| turn | `turn_compaction` | 协作者 | 压缩触发（预算判定 / 策略编排 / cache 影响记账） |
| turn | `turn_dispatch` | 协作者 | call_skill dispatcher 接口与子 runner 构造 |

`child_resume_chain` 与 `spawn_resume.SpawnResumeChain` 是**对称的两条 resume 路径**，互不替代：前者的父 turn 仍在等 `function_call_output` 回填，必须逐层回传；后者的父 turn 早已结束，子 thread 是独立根 turn，无回填链。

## 行为契约

### 切分手法按内聚度二分

- **协作者类**：一组围绕同一入口序列、彼此紧密调用的行为（持宿主引用）。
- **模块级函数**：彼此独立、无共同序列的一次性处理（显式收宿主）。
- 协作者**不自持运行态**——运行态仍由宿主唯一持有，协作者经宿主引用访问。
- 不得为形状统一把独立纯函数硬包成类。

### 宿主是唯一白盒寻址面

抽出的方法在宿主上保留**同名薄委托**，签名从原文逐字截取；协作器内部兄弟调用一律经 `self._<host>._x(...)` **回弹**，不直调本类方法。

两类注入点必须分别照顾，遗漏都会**静默失效而非报错**：

1. **打宿主属性**：`monkeypatch.setattr(engine, "_ttl_expire_after", ...)`、`real = engine._resolve_expiry_route` 取值打桩 → 靠委托 + 回弹保住。
2. **打模块级符号**：`monkeypatch.setattr(engine_module, "TurnRunner", ...)` → 构造点必须留在该模块内，或经 `from taifeng.loop import turn as _turn_mod` 惰性解析。

被拆宿主内定义的符号（`TurnRunner` / `_BatchSuspend`）在协作器内一律惰性解析，同时打破循环 import。宿主对下沉 helper 的再导出必须带 `# noqa: F401`——测试按宿主模块寻址，而 `ruff --fix` 会把「本文件已不再使用」的再导出当垃圾删掉。

### 行为零变化的判据

判据不是「测试通过」，而是**既有测试一行未改即全绿**。任何需要改测试才能过的切片，等于该切片改了行为。

### 具名红线例外

| 对象 | 现状 | 上限 | 理由 |
| --- | --- | --- | --- |
| `engine.py` | 2017 行 | 2100 行 | 薄委托 ~739（94 个）+ `__init__` 参数绑定 ~292 + `run()` ~213 + imports ~124，均无内聚边界可再切；继续下压只能退化为纯转发壳子 |
| `AgentEngine.__init__` | 288 行 | 同上 | 压到 80 行只有 `locals()` 透传（隐式）或改公共构造签名（破契约）两条路，都更糟 |
| `turn_sample` 三段 | 140 / 225 / 221 行 | — | 已按真实接缝切开；再压需把流分派阶段的共享可变量收进状态载体，属最热路径上的设计改动 |

例外随委托面削减或构造期重构相应下调。
