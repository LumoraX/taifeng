# ADR 0062：Anthropic 尾部滚动缓存断点 + TTL 透传

- 状态：Accepted
- 日期：2026-09-28
- 关联：[capabilities/llm-provider-native.md](../architecture/capabilities/llm-provider-native.md)；[capabilities/cache-anchor.md](../architecture/capabilities/cache-anchor.md)；ADR 0004（cache-aware 压缩）/ 0036（cache anchor）

## 背景

内核只在 cache anchor（已被 provider 缓存的前缀末条）上声明一个 `CacheBreakpoint`。Anthropic 只缓存标记处及之前的前缀，
anchor 之后的尾部每次请求都按全价计入输入。工具循环里尾部逐轮变长——第 n 轮要为前 n-1 轮的工具结果再付一次全价。
另外 `CacheBreakpoint.ttl_seconds` 从未被任何 provider 读取，Anthropic 恒用默认 5 分钟；间隔超过 5 分钟的会话（人工审批、
长任务）每轮都要重写缓存。

## 决策

1. Anthropic 客户端默认（`cache_tail=True`）在最后一条消息的最后一个非 thinking 块上再打一个 `cache_control`，下一次请求
   即可按缓存价读取本次完整前缀。只在 Anthropic provider 内做：它是该 provider 的计费策略，不改变 provider-neutral 的
   `CacheBreakpoint` 语义（anchor 仍是「已缓存前缀」的真相）。
2. `ttl_seconds` 透传：300 → 默认，3600 → `ttl: "1h"`，其他值 `InvalidRequestError`（Anthropic 只有两档，不就近取整）。
   同一请求内所有标记用同一 TTL，避免触犯「长 TTL 标记须在短 TTL 之前」的规则；`AnthropicClient(cache_ttl_seconds=)`
   统一覆盖。
3. thinking / redacted_thinking 块不挂标记（服务端签名块须原样回传）。

## 否决的方案

- **在 `build_api_request` 里加第二个 `CacheBreakpoint`**：会把「这里请写缓存」的计费意图混进 anchor 的真相语义，
  且对不支持显式缓存的 provider 毫无意义。
- **默认 1h TTL**：1h 档写入价约为基础价 2 倍，对 5 分钟内连续交互的会话反而更贵；由业务按会话节奏选择。

## 影响

- R2：提升缓存命中，不改变任何前缀内容；anchor 与压缩语义不变。R1 / R3–R5 无变化。
- 行为变化：默认请求多一个 `cache_control` 标记（缓存写入按增量计费、读取按缓存价）。

## 验证

`tests/llm/test_anthropic_cache.py`：anchor + 尾部两标记、首轮只有尾部、关闭尾部、anchor 即末条不重复、断点 1h 透传、
客户端覆盖、非法 TTL（请求期与构造期）、TTL 不一致、thinking 块不挂标记。真实端点未验证（环境无 Anthropic key）。
