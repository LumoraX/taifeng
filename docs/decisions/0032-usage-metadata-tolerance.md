# ADR 0032：usage 记账元数据不得判死一个已成功的 turn（Amends #0026）

- 状态：Accepted（明细读取键那半条已被 [ADR 0034](0034-usage-accounting-never-fails-a-turn.md) 收窄）
- 日期：2026-09-05
- 相关：[ADR 0030](0030-codex-sse-noise-tolerance.md)（同一条「未知形状不得升格为不可恢复故障」原则的
  另一半）；契约见 [Codex Responses Provider 能力契约](../architecture/capabilities/llm-codex-provider.md) §5.2。

## 背景

ADR 0030 放宽了**顶层 SSE 事件 type** 与**行解析层**，但 item 级 / part 级白名单与 `usage` 明细校验
原样保留。实测（2026-09-05 探针）剩下三条「未知 key 直接中断」的路径：

| 形状 | 旧结果 |
| --- | --- |
| output item 是未登记类型（`web_search_call`） | ❌ 中断 |
| message content part 是未登记类型（`output_audio`） | ❌ 中断 |
| **`usage.*_tokens_details` 里出现非整数新字段** | ❌ 中断 |

前两条**可能装着模型的真实输出**，跳过等于静默截断答案——维持 fail closed 是对的。

第三条不同。旧实现是：

```python
if any(not isinstance(v, int) or v < 0 for v in details.values()):
    raise InvalidResponseError(f"Codex usage {key} contains invalid counts")
```

它无差别校验明细里**每一个**值——包括本实现**根本不读**的字段。于是一个
`{"cached_tokens": 5, "note": "hit"}` 就能把一个**已经成功产出内容、已经
`response.completed`** 的 turn 判死；而 `InvalidResponseError` 不可重试、保守 policy 判 TERMINAL，
这个死是终态的。

`*_tokens_details` 恰恰是上游最常扩展的地方（`cached_tokens` / `audio_tokens` / `reasoning_tokens`
都是这么陆续冒出来的）。下一个新字段只要不是非负整数就会再炸一次。

## 决策

**只校验实际会被读取的键**；明细里其余字段、以及整体不是 object 的明细，一律忽略。

- 读取键集合固化为 `_USAGE_DETAIL_READ_KEYS`，与 `extract_usage_openai_family` 的查找集合**一一对应**：
  `input_tokens_details` / `prompt_tokens_details` 的 `cached_tokens`，
  `output_tokens_details` / `completion_tokens_details` 的 `reasoning_tokens`
  （chat 风格别名容器也在内——提取器优先查它们；漏掉会让坏值直落 `int()` 的深处崩出非 LLMError）。
- 这些键仍严格 fail closed：提取器对它们做 `int()`，坏值不挡在这里就会在更深处炸。
- **非 object 的明细容器不再是失败**：提取器本就 `isinstance(..., dict)` 不成立即跳过，读不到任何值、
  按 0 处理——校验比消费更严格是没有道理的。
- **顶层三个计数不变**，维持严格：它们喂 K2 会话 token 天花板等资源决策，错值会导致错误调度。
  `total_tokens == input + output` 的一致性校验同样保留（本次不涉及）。

判据一句话：**能装内容的，宁可炸；纯记账的，绝不许炸。**

## 后果

### 正向

- 上游 / 中转网关扩展 usage 明细不再让 turn 停摆，与 ADR 0030 同一条原则贯通到底。
- 校验范围与消费范围对齐，不再存在「校验了却不读」的字段。
- 别名容器（`prompt_tokens_details` / `completion_tokens_details`）的坏值现在也被挡在
  `InvalidResponseError` 边界内，不会再以裸 `ValueError` 形态从 `int()` 深处冒出来。

### 代价

- 明细里不被读取的字段若真的畸形，本层不再报错——但它对本实现无影响，业务侧仍可从
  `TokenUsage.raw` 拿到原始 usage 自行判读。
- `_USAGE_DETAIL_READ_KEYS` 与提取器构成一处必须手工同步的耦合；改提取器时漏改本表会退回
  「读了却没校验」。已在常量注释里写明。

## 被否决方案

1. **连顶层计数一起放宽**：它们影响资源决策（会话 token 天花板），不是纯展示，否决。
2. **明细一律不校验**：读取键的坏值会在 `int()` 处炸成非 LLMError，错误分类更差，否决。
3. **保留旧校验、只加白名单例外**：白名单要追着上游字段跑，与 ADR 0030 否决「补全事件全集」同理，否决。

## 未纳入本次

- **output item / content part 的未登记类型仍 fail closed**（见背景表前两行）：它们可能承载真实输出，
  且本实现的 wire 从不请求 hosted tool / audio，上游自发出现的概率远低于 usage 加字段。
- `usage` 顶层别名字段（`cache_read_input_tokens` / `prompt_cache_hit_tokens` /
  `cache_creation_input_tokens`）目前完全不校验，坏值会以裸 `ValueError` 从提取器冒出。
  同类隐患，另开切片。

## 验证

- `tests/llm/test_codex_usage_tolerance.py`：26 个用例——7 种未知 input 明细字段、3 种未知 output 明细
  字段、4 种非 object 明细均不中断且读数正确；读取键的 4+3 种坏值仍 fail closed；顶层 4 种坏计数仍
  fail closed。
- 真实回归：`docs/real-llm-ledger.md`。
