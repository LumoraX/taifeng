# Capability: usage-tree-accounting

## Purpose

会话级 token 用量由**整棵 turn 树共享一本账**：根 turn、`call_skill` 阻塞子 turn、detached spawn
子树、挂起后的续跑链，每次采样都**实时**计入同一个 `SessionUsageMeter`，并按 skill / thread 归因。

修复的缺口：此前只有根 turn 收尾时把自身 usage 加进 `_session_tokens`，子树 usage 从不回灌；
子 runner 只拿启动瞬间的基线做 K2 检查，兄弟子树之间互相看不见消耗——`max_session_tokens`
可被子树整体绕过（一个 entry 派 10 个各吃 10k 的子 skill，会话账上只记 entry 自己的几百）。

参照：codex `agent/control/budget.rs`（预算整棵 agent 树共享）。决策：ADR 0044。

实现：`loop/usage_meter.py`（`SessionUsageMeter` / `UsageTally`）、`llm/types.py`（`add_usage`）、
`loop/turn_persist.py`（`accumulate_usage` / `session_tokens_now`）、`loop/turn_dispatch.py`
（子树 usage 归并）、`loop/engine.py`（持有计量器、注入所有 runner、`_session_tokens` 视图）。

## 数据契约

### `SessionUsageMeter`

| 成员 | 含义 |
| --- | --- |
| `total_tokens: int` | 会话累计 total_tokens（跨 turn、含全部子树与续跑） |
| `add(usage, *, thread_id, skill_id)` | 入账一次采样的 usage（非累计值），同时归因 |
| `thread_total(thread_id) -> int` | 某 thread 累计；未出现过为 0 |
| `snapshot() -> dict` | `{"total_tokens", "by_skill": {id: tally}, "by_thread": {id: tally}}` |

`tally` = `{"input_tokens", "output_tokens", "total_tokens", "cache_read_input_tokens", "reasoning_tokens", "samples"}`。
`total_tokens` 缺省时按 `input + output` 补。

### `TurnRunner` 字段

| 字段 | 含义 |
| --- | --- |
| `usage_meter: SessionUsageMeter \| None` | engine 注入的共享计量器；None = 退回「启动基线 + 本 turn」旧口径（仅直接构造 runner 的场景） |
| `total_usage: TokenUsage` | 本 runner **自身**采样累计（语义不变） |
| `subtree_usage: TokenUsage` | 自身 + 阻塞式 `call_skill` 子树累计；detached spawn **不计入**（生命周期可长于父 turn） |

### `turn_completed` 事件新增字段

| 字段 | 含义 |
| --- | --- |
| `subtree_usage` | 同上（`TokenUsage.model_dump()`） |
| `thread_id` | 该 turn 所在 thread |
| `skill_id` | 该 turn 的 entry skill id |

`usage` 仍为自身用量——按 `turn_completed` 逐条求和的既有消费者**不会**重复计数；要整棵阻塞子树
的总量读根 turn 的 `subtree_usage`。

### `engine.introspect()["usage"]`

`= SessionUsageMeter.snapshot()`。`introspect()["session_tokens"]` 与 `usage.total_tokens` 恒相等。

## 行为契约

### Requirement: 采样即入账

每次采样 `completed` 时（`accumulate_usage`），usage SHALL 同步计入 `total_usage`、`subtree_usage`
与共享计量器。**不等 turn 收尾**：turn 失败 / 取消 / 挂起前已花掉的 token 同样在账上。
engine 在 turn 收尾时 SHALL NOT 再补加根 turn usage（否则重复计数）。

### Requirement: K2 读实时总量

注入计量器时，turn 内 K2 检查（`_session_limit_exceeded`）与 pre-turn 守卫 SHALL 读计量器总量。

#### Scenario: 子树用量触顶
- **WHEN** 根 turn 自身用 200、其 `call_skill` 子 turn 用 3000，`max_session_tokens=1000`
- **THEN** 会话累计为 3200，下一个根 turn SHALL 被 pre-turn 守卫拒绝（`turn_refused`）

#### Scenario: detached spawn 用量入账
- **WHEN** 经 `spawn_skill` 起的子 skill 采样消耗 4000
- **THEN** `engine._session_tokens` 与 `introspect()["usage"]["by_thread"][child_thread_id]` 均反映这 4000

### Requirement: 全拓扑注入

根 turn、CompactNow 以外的全部 runner 构造点（根 turn、`call_skill` 子 turn、detached spawn、
子 thread 续跑链）SHALL 注入同一计量器。

## R1–R5 影响

- R1：归因键是 thread_id / skill_id，内核既有概念，无业务字段。
- R2：不触 prompt。
- R3：归因明细经 `turn_completed` 与 `introspect()` 透出。
- R4：同步累加，无阻塞。
- R5：计量器是进程内运行态；冷重载后从 0 起算（与既有 `_session_tokens` 语义一致）。宿主需要跨进程
  延续会话预算时，可在重建后设定 `max_session_tokens` 为剩余额度。
