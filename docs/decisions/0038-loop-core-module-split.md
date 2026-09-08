# ADR 0038：engine / turn 按职责切分为协作者模块；两处具名红线例外

- 状态：Accepted
- 日期：2026-09-08
- 关联：[loop 核心模块结构契约](../architecture/capabilities/loop-core-module-structure.md)；[agent-loop 活文档](../architecture/agent-loop.md)；承 ADR 0006（统一 Skill 抽象）与 `spawn-driver-module-split` 先例；openspec change `wave4-engine-turn-module-split`

## 背景

2026-09-03 全系统审查的 Wave 4「结构」。`src/` 的硬红线是**文件 ≤ 800 行、函数 ≤ 80 行**，而 `loop/` 的两个核心文件长期超限且仍在增长——前三波每一波都改到它们（wave2a 根 turn、wave2b spawn/resume、wave2c cache anchor）：

- `engine.py` **4031 行**（红线 5.0 倍），`AgentEngine.__init__` 单函数 **292 行**；
- `turn.py` **2457 行**（3.1 倍），`_sample_once` 单函数 **532 行**，全仓最长。

后果不是审美问题：4000 行单文件让「改这里会不会碰坏那里」无法靠阅读回答，只能靠全量测试兜底；也让多 session 并发协作的冲突面最大化（`AGENTS.md` 明确 `loop/` 是冲突高发区）。

仓内已有同形先例：spawn 子系统在 `spawn-driver-module-split` 里切成四模块并沉淀出 `spawn-module-structure` 契约（模块边界 + 状态单一持有 + 行为零变化）。本波把同一套做法推到 engine 与 turn。

## 决策

### 1. 切分手法按**内聚度**二分，不按有无状态

一组围绕同一入口序列、彼此紧密调用的行为落为**协作者类**（持宿主引用）；彼此独立、无共同序列的一次性处理落为**模块级函数**（显式收宿主）。

起初写的判据是「有状态→类、无状态→函数」，落地时发现与仓内现状不符：既有协作者 `SpawnResumeChain` / `JoinBarrierCoordinator` 明示「**无自有状态**，运行态全部经 driver 访问」。真实分界是内聚度而非状态归属，故据实改判据。协作者一律不自持运行态——运行态仍由宿主唯一持有。

### 2. 宿主是**唯一白盒寻址面**

抽出的方法在宿主上保留同名薄委托（签名从原文逐字截取），协作器内部兄弟调用一律经 `self._engine._x(...)` / `self._owner._x(...)` **回弹**而非直调本类方法。

这不是洁癖。`spawn_driver` / `spawn_barrier` / `spawn_rewind` / `peer_mailbox` / `spawn_resume` 五个兄弟模块与大量测试按 `eng._load_thread_items` 等原名寻址；测试还做 `monkeypatch.setattr(engine, "_ttl_expire_after", ...)` 与 `engine._resolve_expiry_route` 取值打桩。协作器直调兄弟方法会**绕过注入点，且不报错**——是静默失效。

还有一类**打模块级符号**的注入点须分别照顾：`test_audit_history_merge` 做 `monkeypatch.setattr(engine_module, "TurnRunner", ...)`。因此 `_new_turn_runner` **刻意留在 `engine.py`**，`turn_dispatch` 里的子 runner 构造改用 `from taifeng.loop import turn as _turn_mod` 惰性解析（同时也打破 turn ↔ turn_dispatch 的循环 import）。

### 3. 行为零变化的判据是「测试一行未改」

本波不改任何行为契约。判据不是「测试通过」，而是**既有测试一行未改即全绿**：任何需要改测试才能过的切片，等于该切片改了行为，退回重做。十个切片每片单独提交、每片跑全量。

### 4. 两处具名红线例外

**（a）`engine.py` 上限 2100 行**（现 2017）。切分后其构成可核算：薄委托 ~739 行（94 个）+ `__init__` 参数绑定 ~292 行 + `run()` actor 主循环 ~213 行 + imports ~124 行，四项已逾 1300 且均无内聚边界可再切。继续下压只能把 `run()` 与公共 API 也搬走，使 `engine.py` 退化为纯转发壳子——文件变短，但不再承担任何职责，且新增一层无收益的间接。

`__init__` 的 292 行同属此例外：压到 80 行内只有两条路——`locals()` 透传（以隐式换行数）或改公共构造签名（破坏对外契约），两者都比长函数更糟。

**（b）`sample_once` 三段各约 140 / 225 / 221 行**。已按真实接缝切开（请求构建与预检 / 建会话与流事件分派与落史 / 工具批派发与结算），跨段局部量以 AST 核出的真实跨界集合逐字传递。进一步压到 80 行内需把流分派阶段的共享可变量（已流出文本、推理文本、工具批累加器、终止标记）收进状态载体——那是**在最热采样路径上改设计**，超出本波「行为零变化的模块切分」定位。

两处例外均写入对应能力契约，并随委托面削减或构造期重构相应下调。

## 后果

- `engine.py` 4031 → 2017，`turn.py` 2457 → 768（入红线），新增 19 个模块，全部 ≤ 800。
- 全仓最长函数从 532 降到 288（`AgentEngine.__init__`）。
- 代价是多了 94 + 若干个薄委托与一层间接。这是**换来兄弟模块与测试的寻址稳定**的自觉定价，不是疏漏。
- 留作后续独立变更：削减委托面（让兄弟模块直接寻址协作器）、`sample_once` 的状态载体重构、以及另外四个仍超限的文件（`event.py` 946 / `pool.py` 832 / `spawn_driver.py` 838 / `audit.py` 782 中前三者）。

## 备选方案

- **Mixin 继承**：所有方法名天然留在 `AgentEngine` 上，零委托、engine.py 可再降 700+ 行。否决：仓内无此范式，且 mixin 的隐式 `self` 耦合让「这个方法属于谁」更难回答，与切分初衷相悖。
- **`__getattr__` 动态转发**：可消掉全部委托。否决：破坏类型检查与显式性，属 CLAUDE.md 明令禁止的隐式兜底。
- **让兄弟模块直接寻址协作器**（`eng._gate.x()`）：可省约 550 行。本波否决，因它要求同步改 5 个 src 模块的调用点，与「行为零变化、调用方不动」的判据冲突；留作后续变更单独评估。
