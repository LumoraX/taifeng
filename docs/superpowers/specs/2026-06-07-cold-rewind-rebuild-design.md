# 设计:Turn-Rewind 冷场景重建(跨进程 / 冷 worker 也能 rewind)

- 日期:2026-06-07
- 状态:**设计中**。落地后活文档真相见 [`capabilities/turn-rewind.md`](../../architecture/capabilities/turn-rewind.md) + 新 ADR(标 `Supersedes #0014` 的 node_id 段)
- 前置:本设计**扩展** [`2026-06-05-addressable-dispatch-rewind-design.md`](2026-06-05-addressable-dispatch-rewind-design.md)(v1 热场景 rewind),不推翻其截点 / mode 语义
- 适用红线:R1(业务零侵入)/ R2(cache 友好)/ R3(可观测)/ R4(可取消)/ R5(可 resume)—— 本能力改 rewind 节点表的产生时机与寻址范围,并新增"冷加载逻辑 history 重建",按 CLAUDE.md 逐条声明影响(见 §8)

## 1. 问题陈述

### 1.1 冷场景 rewind 节点表为空

v1 rewind 的回访节点表(`RewindLog`)**只活在内存**,只在热 turn 跑完([`engine.py:801`](../../../src/taifeng/loop/engine.py))与 `CompactNow`([`engine.py:1576`](../../../src/taifeng/loop/engine.py))回写 engine。**没有任何 load / resume 路径重建它**:engine `__init__` 明确**不调** `store.load_thread`,`load_thread` 只用于重建 history。

→ 冷 worker / 跨进程时,即便 history 经 `initial_history` 灌回内存,`rewind_nodes()` 仍空 → 任何 `Rewind` 命中 `unknown_node` 拒绝([`engine.py:1468-1470`](../../../src/taifeng/loop/engine.py))。业务侧"从某一步重跑"在冷场景完全不可用。

### 1.2 更深的根因:持久化 transcript ≠ 内存 history

冷加载要正确,前提是"重载出的 history" 与"热场景内存 history" 一致。**但 append-only 主存在两种情况下与内存发散**:

- **压缩**:`SlidingWindow` / `Handoff` 把内存 `history_buffer[:] = head + [placeholder] + tail`,但只把 placeholder **append 到 JSONL 末尾**([`turn.py:1091-1095`](../../../src/taifeng/loop/turn.py)),被取代的中间 item **不删**(R5 append-only)。`compacted.replaced_range` 记了被替换区间,但**全 src 无任何 load 路径消费它**(grep 证实:只 `sliding.py:80` / `handoff.py:346` 写、models.py 定义)。`load_history` 纯按写入序回放([`transcript.py:144`](../../../src/taifeng/conversation/transcript.py))。→ 重载得 `[head, middle(废弃), tail, placeholder@末尾]`,与内存 `[head, placeholder, tail]` **结构性发散**。
- **历史热 rewind / rollback**:截断只动**内存** `_history`([`engine.py`](../../../src/taifeng/loop/engine.py) `_handle_rewind` / `_handle_rollback`),被截掉的 item 仍物理留在 store,其后是 rewind/rollback marker + re-run 项。→ 重载得 `[...保留, 旧废弃尾, marker, re-run]`,与内存(已截断)发散。

**这同时是个既存隐患**:现有 engine-resume 路径([`pool.py:397-398`](../../../src/taifeng/loop/pool.py))把这份含废弃项的 raw history 喂给 LLM —— resume 一个压缩过的 thread 会把被压缩内容重发一遍(再等下次 pre_turn 重压)。resume 不崩故未被发现,但 rewind 对下标敏感 → 废弃段产节点、`history_len` 落死区、re-run 撞 `t{k}:it{n}` 重复 id,是**正确性 bug**。

→ 因此冷 rewind 的真正前提是先**把 raw transcript 重建成与热内存等价的逻辑 history**(§4),再在其上推导节点表(§5)。

## 2. 目标与非目标

**目标**

- 冷加载一条 thread 后,能为该 thread 的**任意历史 turn** 重建可寻址 rewind 节点表,使 `re_reason` / `retry_tool` 在冷场景与热场景**行为一致**——**包括压缩过 / 历史 rewind 过的 thread**。
- 新增 canonical `reconstruct_logical_history`,把 append-only transcript 重放成与热内存等价的逻辑 history;**顺带修正**现有 resume 对压缩 thread 的废弃项重放隐患。
- 通用内核原语,**零业务概念**(守 R1);不新增独立持久化格式;与任何符合 `MessageStore` 协议(保序 + 完整 + append-only)的后端解耦。

**非目标(本期不做)**

- 不做子 turn 内部节点寻址(延续 v1:只 root turn 入表)。
- 不做"挂起态 turn 内 rewind"(延续 v1 `turn_suspended` 拒绝)。
- 不做并行批次内部分 `retry_tool`(延续 v1:假定 `max_parallel_tool_calls=1`)。
- 不改压缩 / spawn 的物理存储布局(只新增"读时重建",不动写路径除 §4.3 给 marker 补 `cut_index`)。
- 不做 PG / OSS 等 `MessageStore` 后端实现(独立后续变更,见 §10)。

## 3. 方案取舍:持久化重载 vs 从 history 推导

| | 方案 A:持久化 checkpoint | **方案 B:从逻辑 history 推导(采用)** |
| --- | --- | --- |
| checkpoint 存哪 | 写进 JSONL 新 record kind,冷加载读回 | **不存**,从重建后的逻辑 history 现算 |
| 真相来源 | 持久化的 checkpoint 记录 | transcript 重放出的逻辑 history |
| 下标坐标系 | 写时坐标 → 读时含 marker / 废弃项 → **整体偏移** | 推导 / 截断同一套**逻辑 history** → **自洽** |
| 存储格式 | 引入新 record kind,与 append-only 留痕冲突 | 零新增格式 |

**采用方案 B**,但其"自洽"成立的前提是先做 §4 的逻辑 history 重建——直接对 raw transcript 推导会踩 §1.2 的发散。

## 4. 基石:`reconstruct_logical_history`(冷加载逻辑 history 重建)

新增纯函数,落 [`src/taifeng/conversation/`](../../../src/taifeng/conversation/)(归属 history 语义,resume 与 rewind 共用):

```python
def reconstruct_logical_history(raw: list[ResponseItem]) -> list[ResponseItem]:
    """把 append-only transcript 顺序重放成与热内存等价的逻辑 history。

    纯 CPU、无 IO。对未压缩 / 未 rewind 的干净 thread 是恒等映射(向后兼容)。
    """
```

### 4.1 重放规则(按写入序扫一遍,维护 `logical` 列表)

| item kind / source | 动作 |
| --- | --- |
| `system_injection` 且 `source == memory_pre_evict`(压缩 salvage digest) | **暂存式 append**:`logical.append(note)`。它在 store 里紧贴其 placeholder **之前**写入([`turn.py:345`](../../../src/taifeng/loop/turn.py)),但热内存中位于 placeholder **之后**([`turn.py:334-344`](../../../src/taifeng/loop/turn.py) `insert_at = summary_index + 1`)→ 由下一行 `compacted` 规则负责挪位 |
| `compacted`(带 `replaced_range=(s, e)`) | 若 `logical[-1]` 是 `memory_pre_evict` note 则先 `salvage = logical.pop()` 否则 `salvage = None`;再 `logical = logical[:s] + [item] + ([salvage] if salvage else []) + logical[e:]`——丢弃被替换区间、placeholder 折进 `s` 位、salvage note 紧随其后(复现内存 `[head, PH, note, tail]`)。**顺序重放天然支持多次/嵌套压缩**:扫到第二个 placeholder 时 `logical` 已含第一个折叠结果,下标对齐 |
| `system_injection` 且 `source ∈ {rewind, rollback}` | 截断信号:`logical = logical[:cut_index]`(`cut_index` 见 §4.3),**marker 本身不进 `logical`**(热路径 marker 只落 store、不进 `_history`) |
| 其余所有 item(含普通 `system_injection` / `suspend_resolved` marker / `reasoning` / `spawn` 等) | `logical.append(item)`(它们在热内存 history 中存在) |

> **salvage 关联规则的依据**:`_apply_pre_evict_salvage` 一次压缩恰好产一条 `memory_pre_evict` note,且 store 写序里 note(:345)**紧接** placeholder(:1094)之前,中间无其他 append。故"placeholder 处若 `logical[-1]` 是该 note 即挪到其后"是确定性配对,无歧义。无 `memory_store` 时无此 note,规则空转。
>
> **不变量(扩展边界断言)**:此配对依赖"salvage note 后必紧跟 placeholder"——内置 `sliding` / `handoff` 在 `success` 时恒设 `summary_item_id`、placeholder append 受其 guard([`turn.py:1091`](../../../src/taifeng/loop/turn.py)),故恒成立。但自定义 `CompressionStrategy` 若 `success=True` 却 `summary_item_id=None` 又触发了 salvage,会写出**孤儿 note**,使本规则误配到后续无关 placeholder。reconstruct 在 pop 前 SHALL 断言"被 pop 的 note 与当前 placeholder 来自同一压缩"(或退化为:孤儿 note 当普通 system_injection 保留)——在系统边界(自定义策略)显式校验,不静默误配。

> **恒等性(向后兼容)**:干净 thread 无 `compacted`、无 rewind/rollback marker → 每项都走末行 append → `logical == raw`。现有 resume 行为对干净 thread 完全不变,仅压缩 / rewound thread 改为**正确**。

### 4.2 与既有"消费型 marker"的边界

- `suspend_resolved` marker(`source='suspend_resolved'`)**保留**在 `logical`(热路径它进 `_history`,[`engine.py:1385`](../../../src/taifeng/loop/engine.py));其"核销 suspension"的语义由既有 `_find_active_suspension` 扫描处理,**不归 reconstruct**。
- 只有 `rewind` / `rollback` 两个 source 触发截断。新增 source 默认走"保留"分支,**禁止穷举 match**。

### 4.3 配套写路径改动:marker 补 `cut_index`

`_handle_rewind` / `_handle_rollback` 当前 marker 只在**文本**里写 node/数量,**未持久化截断下标**。reconstruct 需要它。三处 additive 改动:

1. **扩 `system_injection()` 构造签名**:现签名 `system_injection(text, *, thread_id, source)` 硬编码 `payload={"text","source"}`([`models.py:158`](../../../src/taifeng/conversation/models.py)),无处放 `cut_index`。加一个可选 `extra: dict | None = None` 合并进 payload(additive,不破既有调用)。
2. **`_handle_rewind`**:marker 补 `cut_index = cut`(该处已算出 `cut` 并 `self._history[:cut]`)。
3. **`_handle_rollback`**:`cut_idx` 当前是局部变量([`engine.py:1698/1704`](../../../src/taifeng/loop/engine.py)),未外传 → marker 补 `cut_index = cut_idx`。

> 旧版本(无 `cut_index`)写出的 rewound/rolled-back transcript:因冷 rewind 是**新能力**,不存在"历史遗留的、需要冷 rewind 的"此类 transcript。reconstruct 遇 `source ∈ {rewind, rollback}` 但缺 `cut_index` 的 marker **显式报错**(不静默猜下标),作为已知边界文档化。

## 5. `derive_rewind_log`:在逻辑 history 上推导节点表

新增纯函数,落 [`src/taifeng/loop/rewind.py`](../../../src/taifeng/loop/rewind.py)。**输入是 §4 重建后的逻辑 history**(= 热内存等价),故下标与 engine 后续 `_history[:cut]` 截断同坐标系、自洽。

### 5.1 turn 序号约定(结构化,derive 与 live 同源)

turn 序号 `k` = **累积 `user_message` item 计数(1-based)**。derive 扫 history 计数;live 侧 **`TurnRunner` 起跑时由其 `history_buffer` 内 `user_message` 数算出 `k`**(root turn 起跑时 buffer 已含本 turn 的 user_message + 此前各 turn 的),传入 `record_*`。二者**按同一规则(`rewind.py` 同一 helper `count_turns(history)`)从同一(逻辑)history 算,天然一致**,不依赖 `engine._turn_index`(后者起始 0、冷加载不回填,直接用会撞号)。

### 5.2 推导状态机(按 `ItemKind` 扫逻辑 history)

| item kind | 动作 |
| --- | --- |
| `user_message` | `k += 1`,重置本 turn 的 iteration(`n`)/ dispatch(`m`)计数;记下"当前 iteration history_len"游标 |
| `assistant_message` | 记 iteration 节点:`history_len` = 该 item 下标(并存入游标),`node_id = t{k}:it{n}`,`n += 1` |
| `function_call` | 记 dispatch 节点:`history_len` = **当前 iteration 游标**(归一,assistant 消息原子;同圈多次派发都用同一游标),`inner_history_len` = fc 下标 + 1,`call_id` / `target_id` / `args_digest` 从 fc payload 取,`node_id = t{k}:disp{m}`,`m += 1` |
| `compacted` | 逻辑 history 里 placeholder 是单个 item,占一下标、不产节点(其代表的旧 turn 已折叠,不可寻址) |
| **其余任意 kind**(`system_injection` / `reasoning` / `suspension` / `function_call_output` / `spawn` / `join_barrier` / `join_barrier_fired` / 未来新增) | **default 分支:计入下标,不产节点**。**禁止穷举 match**,新增 kind 自动落此分支不漏算下标 |

> **同圈多派发**:derive 必须把"当前 iteration 的 `history_len`"作为扫描游标,赋给该圈内**每个** `function_call`(跨越中间的 `function_call_output`),复现 live 的 `iteration_history_len` 归一([`turn.py:823`](../../../src/taifeng/loop/turn.py))。

> **空尾圈边界**:live 每圈采样**前无条件**记 iteration 节点([`turn.py:571`](../../../src/taifeng/loop/turn.py)),但 `assistant_message` 仅当非空才追加([`turn.py:741`](../../../src/taifeng/loop/turn.py));末圈空采样自然终止([`turn.py:423`](../../../src/taifeng/loop/turn.py))**不留 history item** → derive 不产该节点。这是有意且无害的(其截点 == history 末尾 == "继续本 turn")。§6 让 derive 成为热/冷唯一产出方 → 热场景同样不暴露 → 自洽。

> **正确性背书**:iteration 逐项布局的 corner case 由**奇偶校验测试**(§9)锁死:跑真实热 turn(含空尾圈 + 多派发圈),断言 `derive_rewind_log(history) ≡ live RewindLog 记录的节点`。

## 6. node_id 迁移 + 接线

### 6.1 node_id 统一 turn 限定

`record_iteration` / `record_dispatch` 产出从 `it{n}` / `disp{m}` 改为 **`t{k}:it{n}` / `t{k}:disp{m}`**,热冷共用。`RewindCheckpoint` 增 `turn_index: int`(`k`)。

> **破坏性**:业务侧已存的 `it2` 这类 id 会失配。ADR + 契约标 `Supersedes #0014` 的 node_id 段 + 迁移说明。已与业务方确认接受。

### 6.2 冷加载(engine `__init__`)

```python
# 1. 先把 raw transcript 重建成逻辑 history(= 热内存等价),修正 §1.2 发散
self._history = reconstruct_logical_history(list(initial_history)) if initial_history else []
# 2. 在逻辑 history 上推导全 turn 节点表(纯 CPU,不碰 IO,不破「engine 不调 store.load_thread」红线)
self._rewind_checkpoints = derive_rewind_log(self._history)
```

二者皆纯计算。空 `initial_history` → 空 history + 空表,退化为现状。

### 6.3 热路径:覆盖改重算(derive 成为唯一产出方)

热 turn 结束([`engine.py:801`](../../../src/taifeng/loop/engine.py))与 `CompactNow`([`engine.py:1576`](../../../src/taifeng/loop/engine.py))从 `= list(runner.rewind_log.checkpoints)` 改为 `= derive_rewind_log(self._history)`(二者本就先 `self._history = list(runner.history_buffer)` 于 :798 / :1573,derive 看到含刚结束 turn 的完整 history;`CompactNow` 跑在压缩后 buffer 上,`compacted` 折叠一致生效)。

**为何重算而非 extend**:让 `derive_rewind_log` 成为**冷加载 / 热 turn 结束 / CompactNow 三处唯一产出方**,消除 rewind re-run 撞号、`_turn_index` 回填依赖、空尾圈热冷不一致。`live RewindLog` 仅保留用于 turn 执行中 emit `rewind_checkpoint_recorded`(R3),node_id 用 §5.1 同 helper,与 derive 一致(§9 锁定)。

## 7. retry_tool 冷场景:补齐 `_last_resolved`

冷 rewind 的 `re_reason` / `retry_tool` 走 `_handle_rewind` → `_build_and_run_runner(..., list(self._last_resolved or []))`([`engine.py`](../../../src/taifeng/loop/engine.py))。`_last_resolved` 起始 `[]`、只在 live turn 中填,**冷 engine 未跑过 turn 时为空** → 冷 rewind re-run 会**丢失已 resolve 的指令层**,违背 §2"行为一致"。

→ **修复(定方案)**:在 `_handle_rewind` 入口,若 `_last_resolved` 为空且 `_history` 非空(= 冷 engine 首次操作),按 engine **构造时配置的 entry skill** 重 resolve 一次填充 `_last_resolved`,再走重推。选此(惰性 on-rewind)而非 warmup 时机:rewind 可能远晚于 warmup,惰性保证用的是 rewind 当下的指令层(含热更);且只在真正 rewind 时付出 resolve 成本。

- **resolve 锚点**:冷 engine 只有"构造时 entry skill"这一个可用锚,即按它 resolve。**已知边界**:若 thread 历史跨多个不同 entry skill 的 turn,冷 rewind 到旧 turn 时用的是当前构造 entry skill 的指令层,不还原"该旧 turn 当时的 entry skill 指令"——v1 不做 per-turn entry-skill 还原(与"只 root turn 入表"同级的范围约束),文档化。
- 需测试覆盖"冷 rewind 后 re-run 带指令层(`_last_resolved` 非空)"。

> 注:`retry_tool` 的 seed 补跑本身不依赖内存 pending 表——`_complete_seed_call` 从 `history_buffer` 读悬空 fc([`turn.py`](../../../src/taifeng/loop/turn.py)),冷加载 history 已含该 fc,故 seed 机制冷场景天然可用。仅指令层需补。

## 8. R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| **R1 业务零侵入** | `reconstruct_logical_history` / `derive_rewind_log` 全通用,无业务概念;与任何 `MessageStore` 后端解耦。✅ |
| **R2 Cache 友好** | 冷加载跨进程 cache 不可信(engine `__init__` 置 `_cache_anchor_index = -1`)。derive 的 checkpoint `cache_anchor` 填 -1(且 `_handle_rewind` 只读 `self._cache_anchor_index`、不读 cp.cache_anchor,填 -1 纯保险)。rewind apply 走 `cache_break_expected_reason="rewind"`,首采样失效标 **expected**,不计 `unexpected_cache_breaks`。✅ |
| **R3 可观测** | 冷重建后 emit `rewind_table_rebuilt{ thread_id, turn_count, node_count }`;重建若丢弃压缩/rewind 段,emit 计数体现。热路径 `rewind_checkpoint_recorded` 不变。 |
| **R4 可取消** | reconstruct / derive 均同步快速 CPU,无长操作。✅ |
| **R5 可 resume** | 二者只读 history,store append-only 不动;marker 补 `cut_index` 是 additive。**冷重建依赖 `MessageStore.load_thread` 的「保序 + 完整」语义**——见 §8.1。✅ |

### 8.1 MessageStore 契约(冷重建依赖)

> **契约**:reconstruct + derive 依赖 `load_thread(thread_id)` **按写入顺序、完整**吐回所有 `ResponseItem`(append-only,不去重 marker、不丢、不乱序)。不满足则下标定位不可信。默认 `JsonlMessageStore` 天然满足;业务自实现 DB store 时为协议红线。多租户隔离是业务上层包 store 的事,非内核 store 职责(守 R1)。

## 9. 测试矩阵(`tests/loop/test_rewind_cold.py` + `tests/conversation/test_reconstruct.py`)

**reconstruct(新基石)**

- **恒等**:干净 thread → `reconstruct(raw) == raw`。
- **单次压缩(无 salvage)**:构造 `[head, mid, tail, PH@end]`(PH.replaced_range 覆盖 mid)→ `reconstruct` == `[head, PH, tail]`,且与"热场景同样压缩后的内存 history" 逐项相等。
- **压缩 + salvage digest(关键)**:`memory_store` 注入,store 尾为 `[..., note, PH]` → `reconstruct` == `[head, PH, note, tail]`,与热内存 `_apply_pre_evict_salvage` 结果逐项相等(验证 §4.1 挪位规则)。
- **多次/嵌套压缩**:两次压缩 → 顺序折叠正确。
- **历史 rewind**:`[...保留, 废弃尾, rewind_marker(cut_index=K), re-run]` → `reconstruct` == `[保留[:K], re-run]`。
- **历史 rollback**:`rollback_marker(cut_index=K)` → `reconstruct` 截到 K(验证 §4.3 rollback 也补了 cut_index)。
- **缺 cut_index**:旧式 rewind/rollback marker(无 cut_index)→ 显式报错,不静默。
- **resume 回归**:压缩 thread resume 后 `history_snapshot()` 不含废弃项(修正既存隐患);跑现有 `test_engine_resume.py` 全绿。

**derive + 冷 rewind**

- **奇偶校验(核心)**:真实热 turn(N 圈 M 派发 + 一个空尾圈 + 一个同圈多派发圈)→ `derive(history) ≡ live RewindLog`(node_id / turn_index / history_len / inner_history_len / args_digest 全等)。
- **冷重建 re_reason / retry_tool**:跑完一 turn → 新建 engine 灌 `initial_history` → 对 `t1:it2` / `t{k}:disp{m}` 成功重跑。
- **冷 rewind 后指令层生效**:验证 §7 修复(`_last_resolved` 非空,re-run 带指令)。
- **多 turn 可寻址**:两 `user_message` 的 thread → 冷加载能 rewind 到 `t1:` 节点。
- **冷加载后再跑新 turn·号不撞**:冷加载 N turn → 跑新 turn → `t{k}:` 前缀严格递增、无重复,新 turn = `t{N+1}:`。
- **压缩过的 thread 冷 rewind**:压缩后冷加载 → 能 rewind 到压缩边界**之后**的节点;对被折叠 turn 的 id 得 `unknown_node`。
- **自定义 MessageStore 后端**:mock 非 JSONL store 跑通冷重建(守 §8.1)。
- **空 / 退化**:空 `initial_history` → 空,不报错。

## 10. 文档落档(收尾红线)

- 更新契约 [`capabilities/turn-rewind.md`](../../architecture/capabilities/turn-rewind.md):node_id 升级 `t{k}:`、新增「冷场景重建」Requirement、v1 边界清单移除「冷重建不支持」。
- 更新 [`agent-loop.md`](../../architecture/agent-loop.md):冷加载 reconstruct + derive 接线、热路径重算。
- 更新 [`conversation.md`](../../architecture/conversation.md) / [`context-compression.md`](../../architecture/context-compression.md):`reconstruct_logical_history` 消费 `replaced_range` + rewind/rollback marker `cut_index`,修正 resume 废弃项重放。
- 新增 ADR:`Supersedes #0014` 的 node_id 段;记录"读时重建逻辑 history(而非写时改布局)"的取舍。

## 11. 后续独立变更(不在本期)

**基础 `MessageStore` plugin(PG / OSS 等)** —— 让 taifeng 自带通用存储后端适配器。需单独 brainstorming,核心待定:落点 A(内核可选 extra,改 R5 措辞)vs 落点 B(独立伴生包 `taifeng-stores`);无论哪种**适配器内禁止业务概念**(无 `tenant_id` / 无领域名词,连接串 / bucket 构造时注入,`src/` 内禁 `os.getenv`);自身议题含 schema / 迁移 / 保序并发写 / OSS 最终一致性 vs append-only。
