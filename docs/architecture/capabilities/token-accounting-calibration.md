# Capability: token-accounting-calibration

## Purpose

让内核对「当前上下文占了多少 token」的判断**以 provider 实测为准**，而不是只靠 `len/3.5` 粗估。
压缩触发、预算提示、发送前预检、`engine.estimate_tokens()` 全部依赖这个数；它不准，压缩要么过早
（浪费 cache），要么过晚（撞上 provider 上限，靠 overflow 自愈多烧一次失败请求）。

粗估的系统性盲区：看不到 system prompt、工具 schema、协议模板开销；CJK 文本按 3.5 字符 / token
严重低估。实测 usage 天然覆盖全部。

参照：codex `context_manager/history.rs`（上次真实 usage + 之后新增条目估算）、
opencode `session/overflow.ts`（按真实 tokens 判断并扣输出预留）。决策：ADR 0043。

实现：`context/budget.py`（`TokenCalibration` / `build_token_calibration` /
`calibrated_history_tokens` / `ContextBudget.output_reserve_tokens`）、
`loop/turn_context.py`（`history_token_estimate` / `calibrate_from_usage`）、
`loop/turn_sample.py`（采样成功后建锚点）、`loop/turn_compaction.py`（压缩改写前缀时失效）、
`llm/providers/_shared.py`（`extract_usage_anthropic` 口径归一）。

## 数据契约

### `TokenUsage.input_tokens` 口径（跨 provider 统一）

`input_tokens` SHALL 表示**完整 prompt token 数（含缓存命中与缓存写入部分）**：

| provider | 映射 |
| --- | --- |
| OpenAI Chat / Responses / Codex / DeepSeek / litellm | `prompt_tokens` / `input_tokens`（上游本就含缓存） |
| Gemini | `promptTokenCount`（本就含缓存） |
| Anthropic | `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`（上游 `input_tokens` 只含未命中部分，内核归一） |

`total_tokens = input_tokens + output_tokens`（Anthropic）。原始字段保留在 `TokenUsage.raw`。

### `TokenCalibration`（`@dataclass(frozen=True)`）

| 字段 | 含义 |
| --- | --- |
| `anchor_len: int` | 实测那次请求发出时的 history 长度；`-1` = 锚点失效 |
| `anchor_item_id: str \| None` | 发出时 history 末项 id（`anchor_len == 0` 时 None），用于前缀校验 |
| `prompt_tokens: int` | provider 实测完整 prompt token 数 |
| `overhead_tokens: int` | `max(0, prompt_tokens - 粗估(发出时 history))`：system / 工具 / 模板开销 + 粗估误差 |

`anchor_valid` = `anchor_len >= 0`；`invalidated()` 返回锚点失效、保留 overhead 的副本。

### `ContextBudget.output_reserve_tokens: int = 0`

为模型输出预留的 token。`usable_input_window = context_window - output_reserve_tokens`；
`soft_limit` / `hard_limit` 按 `usable_input_window × ratio` 计算。默认 0（阈值与引入前一致）。
构造期校验：`< 0` 或 `>= context_window` → `ValueError`。可经 `UpdateBudget(output_reserve_tokens=...)` 运行时调整。

## 行为契约

### Requirement: 三档估算，精度从高到低

`calibrated_history_tokens(items, calibration, estimate=...)`：

1. 锚点有效、`len(items) >= anchor_len`、`items[anchor_len-1].id == anchor_item_id`
   → `prompt_tokens + 粗估(items[anchor_len:])`；
2. 有校准但锚点失效或前缀不符（压缩改写 / rewind 截断 / 前缀替换）→ `粗估(items) + overhead_tokens`；
3. 从未校准 → `粗估(items)`（旧行为）。

#### Scenario: 采样后估算走实测
- **WHEN** 一次采样成功，provider 回报 `input_tokens=5000`
- **THEN** 其后 `engine.estimate_tokens()` = 5000 + 该次请求之后新增条目的粗估

### Requirement: 锚点建立 = cache anchor 推进的同一时刻

- 流正常完成（与 `cache_anchor_index = sent_history_len - 1` 同一处）SHALL 以本次采样的实测
  `input_tokens` 刷新锚点，锚点位置 = `sent_history_len`。
- 实测 `input_tokens <= 0`（provider 未回报 usage）SHALL 保留旧锚点——**无实测不伪造实测**。
- `LLMError` / overflow / 取消路径不经此处，不刷新。
- `overhead_tokens` 下限 0：粗估偏高时不做负修正（宁可高估触发压缩，不低估撞上限）。

### Requirement: 压缩改写前缀使锚点失效

压缩成功应用后，若 `result.cache_invalidated` 或 `result.anchor_preserved_until < anchor_len - 1`，
锚点 SHALL 失效（`invalidated()`），overhead 保留。仅动锚点之后 tail 的压缩不失效——前缀未变，
tail 部分本就按粗估重算。

### Requirement: 单一口径

压缩触发（`TurnCompaction`）、预算提示（budget-awareness）、发送前预检（`ContextBudgetExceeded`）、
`engine.estimate_tokens()` / `usage_ratio()` / `introspect()` SHALL 走同一 `calibrated_history_tokens`。

### Requirement: 跨 turn 携带、跨进程不携带

Engine 持有 `_token_calibration`，每个根 turn / CompactNow runner 注入并在收尾读回（与
`cache_anchor_index` 同一回写点）。冷重载 / 进程重启后为 None，首次采样后重建（R5：不持久化，
重建成本是一次采样）。子 skill（`call_skill` / spawn）的 runner 各自从 None 开始（独立 history）。

## R1–R5 影响

- R1：无业务概念；预留值是业务注入的策略。
- R2：只读 usage，不改 prompt；无 cache 影响。
- R3：估算值随既有 `budget_hint_injected` / `context_budget_exceeded` / `compaction_started` 事件透出。
- R4：纯计算，无阻塞。
- R5：锚点不持久化，resume 后退回粗估直到首次采样。
