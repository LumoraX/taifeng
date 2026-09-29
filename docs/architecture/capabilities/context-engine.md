# Capability: context-engine

> 状态：Experimental（opt-in）。关联 ADR 0017（规则①③）、0093。
> 实现：`src/taifeng/context/engine.py`（协议、校验、参考实现）、`src/taifeng/loop/turn_view.py`
> （视图的装配与缓存）、`context/compressor.py`（`CompressionOrchestrator.context_engine`）。
> 经 `taifeng.experimental` 导出。

## Purpose

由业务决定每次采样把哪些内容发给模型。内核默认发送完整的逻辑 history，超预算时靠压缩策略
改写 history——被折叠的内容不再可取。ContextEngine 提供非破坏性的另一条路：**history 不动，
只改发出去的视图**。

## 不变量

1. **history 是事实**：引擎 MUST NOT 修改 history；内核 MUST NOT 把视图写回 history、store 或回访节点。
2. **默认不启用**：未注入引擎时每次采样发送完整 history，行为与此前逐字节一致。
3. **视图结构合法**：视图里的工具调用与结果 MUST 成对（history 里本来就悬空的调用除外）。
   不合法的视图使 turn 失败，MUST NOT 退回完整 history。
4. **同一 history 版本只装配一次**：预算判定、压缩触发与随后的采样看到的是同一份视图。
5. **预算按视图算**：引擎给出了不同于 history 的视图时，预算提示与压缩触发的占用 SHALL 按视图估算。

## 数据契约

### `ContextEngine`（Protocol）

| 成员 | 契约 |
| --- | --- |
| `name: str` | 引擎名，进事件 |
| `async assemble(request) -> AssembledContext \| None` | 装配这一次采样的视图；`None` = 原样发送完整 history。MUST 可取消（R4） |
| `async after_turn(update) -> None` | 一轮结束后的通知。尽力而为：抛出的异常记录后忽略 |

### `AssembleRequest`

| 字段 | 含义 |
| --- | --- |
| `thread_id` / `entry_skill_id` | 所在 thread 与其上跑的 skill。根 thread、`call_skill` 子 skill、分离派发的 child 各自装配 |
| `history` | 完整的逻辑 history（不可变序列） |
| `budget` | 本 turn 生效的上下文预算 |
| `history_tokens` | 完整 history 的 token 粗估 |
| `cache_anchor_index` | `history` 里已被缓存的前缀的末项下标；`-1` = 没有 |
| `cancel` | 取消 token |

### `AssembledContext`

| 字段 | 含义 |
| --- | --- |
| `items` | 这次采样发给模型的条目，按发送顺序。可以包含 history 里没有的条目 |
| `cache_invalidated` | 相对上一次发出的视图，已缓存的前缀是否被破坏（R2） |
| `anchor_preserved_until` | `items` 里与上一次视图保持一致的前缀的末项下标；`-1` = 没有。内核据此放置缓存断点 |
| `detail` | 引擎自报的结构化计数，随事件透出 |

### `TurnUpdate`

`thread_id`、`entry_skill_id`、`new_items`（本轮由 runner 产出的条目；触发本轮的用户消息在 turn 开始前
已进 history，不在其中）、`history`（本轮结束时完整的逻辑 history）。

### `validate_view(engine_name, history, assembled)`

| 条件 | 结果 |
| --- | --- |
| history 非空而视图为空 | `ContextEngineError` |
| 视图里有 history 里没有的悬空工具调用 / 结果 | `ContextEngineError`，列出 call id |
| `anchor_preserved_until` 不在 `[-1, len(items) - 1]` 内 | `ContextEngineError` |

### `TailWindowContextEngine(*, keep_last_turns)`

参考实现。视图 = 第一条用户消息及其之前的条目 + 最近 `keep_last_turns` 轮（一轮从一条用户消息开始）。

- 轮数不超过 `keep_last_turns + 1` 时返回 `None`（原样发送）。
- 窗口起点相对上一次移动了 → `cache_invalidated=True`、`anchor_preserved_until` 指向开头段的末项；
  没动 → `cache_invalidated=False`，整个视图都是稳定前缀。窗口按 thread 分别记。
- `detail = {"dropped": <被略去的条目数>, "kept_turns": <keep_last_turns>}`。
- `keep_last_turns < 1` → 构造期 `ValueError`。

## 行为契约

### 启用

`EnginePool.create(context_engine=<ContextEngine>)`。引擎随 `CompressionOrchestrator` 到达每一个 runner；
直接构造协调器时用 `CompressionOrchestrator(strategies, context_engine=...)`。

### 一次采样

```
history 版本（长度 + 末项 id）变了？
  ├─ 否 → 沿用已装配的视图
  └─ 是 → engine.assemble(request)
           ├─ 抛异常        → ContextEngineError（turn 失败）；被取消时原样上抛
           ├─ None          → 发送完整 history
           └─ AssembledContext
                → validate_view
                → cache_invalidated 时把下一次 cache 失效记为预期内（原因 context_engine）
                → emit context_assembled
预算提示 / 压缩触发 / 发送前预检：占用 = 视图的粗估（未装配出视图时沿用 history 的校准估算）
build_api_request(history=视图, cache_anchor_index=anchor_preserved_until)
```

- 发出的是视图时，provider 回报的实测输入量对应的是视图，不用于校准 history 的估算。
- 压缩策略照常存在：视图仍超过阈值时压缩改写的是 history，之后 history 版本变化、视图重新装配。
  引擎把视图压在预算之内，压缩就不会被触发。
- 注入了引擎而没有任何压缩策略时，不会发起压缩尝试。
- 预热（[prewarm](prewarm.md)）交给模型侧的请求同样经过视图装配。

### 失败

| 情形 | 结果 |
| --- | --- |
| `assemble` 抛异常 / 视图不合法 | `turn_failed`，`kind = "ContextEngineError"`；本次采样不发出，history 不受影响 |
| `after_turn` 抛异常 | 记日志后忽略，turn 结果不变 |

### 事件

`context_assembled`：`engine`、`history_items`、`view_items`、`view_tokens`、`cache_invalidated`、
`anchor_preserved_until`、`detail`。同一 history 版本只发一次；引擎返回 `None` 时不发。不进 LLM 视图。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| **R1** | 取舍口径全在注入的引擎里；请求只带通用上下文 ✅ |
| **R2** | 引擎显式声明 `cache_invalidated` 与 `anchor_preserved_until`；内核据此放缓存断点并归因 cache 失效。视图不触发压缩，本能力自身不返回 `CompressionResult` ✅ |
| **R3** | `context_assembled`；失败经 `turn_failed` 透出 ✅ |
| **R4** | `assemble` 接收取消 token；取消引发的异常原样上抛，不包成引擎错误 ✅ |
| **R5** | history 与 store 不变，视图不落盘；冷恢复后由引擎对同一 history 重新装配 ✅ |

## 边界

- 不接管压缩：压缩归 `CompressionStrategy`。
- 不改 system prompt：指令层归 instructions-injection。
- 审计模式不支持：发给模型的内容不再等于 Journal 里的会话内容；注入时构造期拒绝
  （`audit_context_engine_unsupported`）。
- 内核不校验视图的语义（是否漏掉了关键信息、顺序是否合理）；Responses 协议下推理项与调用的
  搭配关系同样由引擎负责，不满足时由请求组装阶段的既有校验拒绝。
