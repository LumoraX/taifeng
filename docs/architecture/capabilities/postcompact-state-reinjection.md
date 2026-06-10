# Capability: postcompact-state-reinjection

## Purpose

压缩成功后把 **agent-owned 状态**（规划/任务清单/工作集指针等业务自有上下文）自动**钉回 history 尾部**——解决「摘要吸收了状态、LLM 压缩后忘了自己正在做什么」。参照 hermes `conversation_compression.py` 压缩后重注入 `todo_snapshot` 的范式；差异：① **协议化**（taifeng 不内置任何状态语义，todo 只是业务范例）；② **双层护栏**（per-source + 总预算，防 pinned 反噬压缩）；③ **与 K3 正交**（MemoryStore 是换出抢救 swap-out，本能力是钉回保活 pin-back，两钩子相邻叠加不合并）。

第三轮对比分析 P1 缺口 E1。实现：`src/taifeng/context/pinned_state.py`（协议 + 注册表）、`loop/turn.py`（`_reinject_pinned_state`，紧随 K3 salvage 之后）、`loop/event.py`（`PinnedStateReinjected`）、`loop/engine.py` / `loop/pool.py`（注入面 + 运行时增删）。

## 数据契约

### `PinnedStateSource`（Protocol，业务实现，**同步**）

| 成员 | 含义 |
| --- | --- |
| `name: str` | 事件/审计标识，registry 内唯一（同名注册 `ValueError`，禁静默覆盖） |
| `max_chars: int` | 单 source 渲染上限；超出 `truncate_middle` 截断（保头尾） |
| `format_for_injection() -> str \| None` | 渲染当前状态；`None` = 本次不注入 |

**同步而非 async 是职责边界的表达**：渲染应为纯内存格式化；需要 IO 的长期状态属 K3 `MemoryStore`（async swap）职责。

### `PinnedStateRegistry`

`register(source)`（同名 `ValueError`）/ `unregister(name)`（缺失 `KeyError`，显式失败）/ 按注册序迭代 / `render_all() -> PinnedRenderResult{entries, dropped, errors}`。总预算 `total_max_chars`（默认 8000）按**注册序**累计——先注册优先，装不下的 source **整体丢弃**进 `dropped`（不截断到一半、不静默）。

### 注入形态

复用既有 `system_injection` ResponseItem kind，`source="pinned:<name>"`，**压缩后历史末尾追加**（cache anchor 之后，R2 无额外 break）；逐条 `store.append` 持久化（R5）。上一轮注入的 pinned 项在下一轮压缩中是普通历史（可被摘要吸收/滑窗丢弃），**不做主动清除**——靠压缩自然回收，护栏保证每轮增量有界。

### `PinnedStateReinjected` 事件（`kind="pinned_state_reinjected"`）

`data = {"sources": [{"name", "chars"}], "total_chars", "dropped": [name], "phase"}`——不带渲染正文（PII 约束）；phase 透传压缩相位（pre_turn / mid_turn / manual / overflow）。无 source 注册或全部渲染 `None` 时**不 emit**（零噪声）。console sink 专用渲染（`comp 📌`）。

## 行为契约

### Requirement: 注入点 = `_maybe_compress` 成功分支、紧随 K3 salvage 之后
- strategy-agnostic：任何成功压缩（handoff / sliding / surgical / overflow 自愈 force_compress / 手动 CompactNow）都触发；压缩失败 / 配对回滚（G1b）/ hook deny 跳过的轮次**不**注入。
- 两个「压缩瞬间钩子」相邻可见：K3 salvage digest 插在 summary 之后，pinned 项追加在最尾，顺序稳定（`memory_pre_evict` 先于 `pinned:*`）。
- 注：manual `CompactNow` 路径既有设计不带 `memory_store`（K3 salvage 只在 turn 内压缩接缝生效），但 pinned 注入两条路径均覆盖。

### Requirement: 双层护栏与显式丢弃
- per-source 超 `max_chars` → `truncate_middle` 截断；registry 总预算装不下 → 整 source 跳过 + 事件 `dropped` 如实记录。

### Requirement: 业务渲染崩溃不传染（非 silent fallback）
- 单 source `format_for_injection` 抛异常 → 捕获 + `EngineLog`（level=warning，含 source 名）告警 + 跳过该 source；其余 source 与压缩结果本身不受影响。

### Requirement: 注入面与运行时增删（R1）
- 构造期：`EnginePool.create(pinned_state_sources=[...], pinned_total_max_chars=…)` → `AgentEngine` 同名参数 → registry 为 engine 级共享实例，透传所有 TurnRunner 构造点（4 处）。
- 运行时：`engine.register_pinned_state(source)` / `engine.unregister_pinned_state(name)`（宿主装配动作，业务持 engine 引用直调，不走 Op），生效于下一次成功压缩。
- 默认 `None`/空注册表 → turn 层短路，**零行为变化**（迁移即回滚 = 不注册）。

## R1–R5 影响

- **R1**：✅ 内核不含任何状态语义；source 渲染与注册全部业务侧。
- **R2**：✅ tail 追加在 anchor 之后；压缩本就是 expected break，不引入新的 unexpected break。
- **R3**：✅ `pinned_state_reinjected` 事件（不带正文）+ 渲染异常 `EngineLog` 告警 + console 渲染。
- **R4**：⚪ 纯内存同步渲染，无长时操作。
- **R5**：✅ pinned 项经 `store.append` 持久化；resume 重载历史含 pinned 项（e2e 守护）。

## 测试

`tests/context/test_pinned_state.py`（8：注册序/同名拒绝/注销/截断/预算丢弃/None 跳过/异常捕获）、`tests/loop/test_pinned_reinjection.py`（5：尾部注入 + 持久化 + 事件契约/三 source 预算溢出/None+异常混合/全 None 零噪声/压缩失败不注入）、`tests/loop/test_pinned_engine_wiring.py`（4 e2e：构造期注入 + CompactNow/运行时增删/R5 resume 存活/K3 双钩子叠加顺序）、`tests/loop/test_turn_overflow_recovery.py`（overflow 自愈路径 phase=overflow 注入）。零注册零变化由全量回归守护。

## 内置范例:todo builtin(已落地)

`tool/builtins/todo.py`:`TodoStore`(直接实现本协议,name="todo",空清单渲染 None)+ `make_todo_write_tool(store)`(`todo_write` 工具,**整表替换**语义对标 Claude Code TodoWrite,入参违例 bad_args 显式拒绝,`parallel_safe=False`)。装配 = 同一实例双注入:`extra_tools=[make_todo_write_tool(store)]` + `pinned_state_sources=[store]` —— LLM 自管清单自动穿越压缩,无新增事件。不做持久化(进程内状态;R5 由注入项落史保证)、不默认注册。测试 `tests/tool/test_todo_builtin.py`(8)。

> demo:`examples/compression_showcase/pinned_demo.py`(业务自实现 source)与 `todo_demo.py`(内置 todo builtin),均 mock 可跑。
