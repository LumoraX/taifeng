# ADR 0034：usage 记账取不到就置空，不得打断基础业务（Supersedes #0032 明细部分）

- 状态：Accepted
- 日期：2026-09-06
- 相关：[ADR 0030](0030-codex-sse-noise-tolerance.md)（SSE 未知帧不得升格为故障）；
  [ADR 0032](0032-usage-metadata-tolerance.md)（本 ADR 收窄其「明细读取键仍 fail closed」的那半条）；
  契约见 [Codex Responses Provider 能力契约](../architecture/capabilities/llm-codex-provider.md) §5.2。

## 背景

ADR 0032 把 `usage.*_tokens_details` 里**我们不读的**字段改为忽略，但对**实际会读取的**键
（`cached_tokens` / `reasoning_tokens`）维持 fail closed。当时给出的理由只有一条：

> 提取器对它们做 `int()`，坏值会在更深处炸出非 LLMError。

也就是说，那道闸门存在的**唯一目的**是把深处的裸崩转成分类错误——它本身并不认为这些值重要。

同时，`usage` 顶层的三个别名字段（`cache_read_input_tokens` / `prompt_cache_hit_tokens` /
`cache_creation_input_tokens`）根本没有任何校验，直接落到提取器的 `int()`。实测：

| 中转站发的值 | 旧结果 |
| --- | --- |
| `cache_read_input_tokens: "abc"` | ❌ 裸 `ValueError`（不是 LLMError，失败策略分不了类） |
| `prompt_cache_hit_tokens: {"x": 1}` | ❌ 裸 `TypeError` |
| `cache_creation_input_tokens: "N/A"` | ❌ 裸 `ValueError` |
| `input_tokens_details.cached_tokens: "0"` | ❌ 判死整个 turn（闸门） |
| `input_tokens_details.cached_tokens: 0.0` | ❌ 判死整个 turn（闸门） |
| `input_tokens_details.cached_tokens: null` | ❌ 判死整个 turn（闸门） |

后三行尤其要命：**这些不是坏数据，是表示法差异**。JSON 里把数字发成字符串、整数发成浮点、
缺省发成 `null`，任何一个上游改一下序列化就中招——而这些值我们只拿来算 cache 命中率。

## 决策

按「**报错只依据正式规范里必填、且我们据以做决策的字段；其余取不到就置空**」重划边界。

**一、主计数 `input_tokens` / `output_tokens` / `total_tokens` —— 维持 fail closed。**
它们是 Responses / Chat 规范的必填整数，直接喂会话 token 天花板等资源决策，错值会让调度判断
出错。但抛的必须是**分类过的** `InvalidResponseError`，不再让 `int()` 裸崩——裸 `ValueError` /
`TypeError` 不是 `LLMError`，失败策略拿不到 SUSPEND / TERMINAL 处置。

**二、一切记账量 —— 取不到就置空（0），绝不判死 turn。**
覆盖 `cache_read_input_tokens` / `cache_creation_input_tokens` / `prompt_cache_hit_tokens` /
`*_tokens_details.cached_tokens` / `*_tokens_details.reasoning_tokens`。它们要么不是 OpenAI
正式顶层字段（Anthropic 风格 / DeepSeek 特有），要么是可选明细；**不影响输出正确性、不驱动
任何决策**。每个字段首次出现不可用值时告警一次，之后静默。

判定「可用」的口径，宽在表示法、严在语义：

| 值 | 结果 | 为什么 |
| --- | --- | --- |
| `"128"`（数字字符串） | 采用 128 | 表示法差异，不是坏数据 |
| `1.5` / `0.0`（浮点） | 截断采用 | 同上 |
| `-1` | 置 0 | token 计数不可能为负，放行会污染 `cache_hit_ratio` |
| `True` | 置 0 | `bool` 是 `int` 子类，`int(True)==1` 会把布尔标志蒙混成计数 |
| `"abc"` / `{...}` / `null` | 置 0 | 解析不了 |

**三、`_strict_usage` 的明细闸门整体删除**（连同 `_USAGE_DETAIL_READ_KEYS` 与
`_non_negative_count`）。提取器自己容忍之后，深处不再炸，闸门只剩副作用。

## 后果

- 中转网关的序列化差异不再打断已经产出内容、已经 `response.completed` 的 turn；
- 上游给 `usage` 加新字段 / 改表示法不会让内核当场报错（与 ADR 0030 同一条原则）；
- 代价：记账数字可能偏低（不可用即计 0），`cache_hit_ratio` 会相应偏保守。这是可接受的——
  记账偏差只影响观测，判死 turn 影响业务。
- 对 R1–R5 的影响：无。不涉及压缩 / cache anchor / dispatch / 取消 / resume。

## 备选方案

- **把三个顶层别名字段也纳入严格校验**：与本 ADR 相反的方向。否决理由——上游随时加新字段 /
  改表示法，把不认识的一律判死等于新功能一上线内核就当场报错，正是 2026-09-05 keepalive
  故障的形态（ADR 0030）。
- **维持 ADR 0032 的明细闸门，只修顶层别名字段的裸崩**：不彻底。闸门的立论前提（深处会裸崩）
  在修完提取器后已不成立，留着只会继续因表示法差异判死 turn。
