# Capability: prewarm

> 状态：Experimental（opt-in）。关联 ADR 0017（规则①③）、0092。
> 实现：`src/taifeng/loop/engine_prewarm.py`（执行）、`src/taifeng/llm/prewarm.py`（模型侧协议与参考实现）、
> `loop/submission.py`（`Prewarm`）、`loop/turn_sample.py`（`preview_request`）。
> `Prewarm`、`ModelPrewarmer`、`PrewarmOutcome`、`CachePrimingPrewarmer` 经 `taifeng.experimental` 导出。

## Purpose

会话的第一次采样最慢：指令层要解析，连接要建，system prompt 与工具清单这段静态前缀要被 provider
首次处理。预热把这些开销挪到用户输入到来之前。

## 不变量

1. **不留痕迹**：预热 MUST NOT 改 history、占用 turn 序号、写 store、登记回访节点、产生 turn 事件。
2. **让路**：用户消息到达时，未完成的预热 SHALL 被取消；真实的 turn MUST NOT 等预热跑完。
3. **失败不传染**：任何步骤失败只体现在 `prewarm_completed` 里，之后的 turn 照常进行。
4. **前缀一致**：交给模型侧预热的请求与下一次采样的请求经同一套组装，system prompt、工具清单、
   已有的 history 逐项相同。
5. **默认无模型侧动作**：未注入 `ModelPrewarmer` 时 `model` 步骤为 `unsupported`，不发任何请求。

## 数据契约

### `Prewarm` Op（kind=`prewarm`）

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `steps` | `("instructions", "working_set", "model")` | 要做的步骤，按给定顺序执行；未知步骤构造期拒绝 |

| 步骤 | 做什么 | 结果取值 |
| --- | --- | --- |
| `instructions` | 解析 engine / session / turn 三档指令层 | `resolved`；没有解析器为 `skipped` |
| `working_set` | 由已有战绩重算工作集（[skill-working-set](skill-working-set.md)） | `restored`；未启用为 `skipped` |
| `model` | 组装下一次采样会发出的请求，交给 `ModelPrewarmer` | `primed`；未注入预热器或它报告无事可做为 `unsupported`；会话 token 已触顶为 `skipped` |

任一步骤还可能是 `failed`（抛出异常）或 `cancelled`（轮到它时预热已被取消）。

### `ModelPrewarmer`（Protocol）

`async prewarm(request: ApiRequest, *, cancel) -> PrewarmOutcome`。

- `request`：下一次采样会发出的请求，不含尚未到来的用户输入；实现 MUST NOT 修改它。
- 实现 MUST 可取消（R4）。
- 抛出异常即预热失败，内核记录后继续。

### `PrewarmOutcome`

| 字段 | 含义 |
| --- | --- |
| `primed` | 是否真的做了预热 |
| `usage` | 消耗的 token；没有消耗为 `None` |
| `detail` | 给运维看的说明 |

### `CachePrimingPrewarmer(model_client, *, probe_text="ping", max_output_tokens=1)`

参考实现：在请求末尾追加一条探针用户消息、把输出上限压到 `max_output_tokens`、去掉结构化输出要求，
发一次采样并丢弃回答。前缀（system prompt、工具清单、已有输入项）不变。**会消耗 token**。
`probe_text` 为空或 `max_output_tokens < 1` → 构造期 `ValueError`。

### 启用

`EnginePool.create(model_prewarmer=<ModelPrewarmer>)`；之后 `engine.submit(Prewarm())`。
只想预热指令层与工作集时不必注入预热器：`Prewarm(steps=("instructions", "working_set"))`。

## 行为契约

```
Prewarm 提交 → emit prewarm_started{steps}
  → 登记在飞预热（可被 CancelTurn 取消）→ 排队取 root gate
  → 逐步执行；某步失败记 failed 后继续下一步；被取消后其余步骤保持 cancelled
  → 释放 root gate → emit prewarm_completed
```

- **与 turn 互斥**：预热持有 root gate。turn 在跑时提交的预热排在它后面，看到的是 turn 结束后的 history。
- **让路**：用户消息开始排队取 gate 之前，先取消全部在飞的预热；预热的 `prewarm_completed`
  先于该 turn 的 `turn_started`。
- **指令层**：预热解析出的指令用于组装预热请求；首轮 turn 仍自行解析，解析器有缓存时命中缓存。
- **会话账**：`PrewarmOutcome.usage` 记进会话用量（归因到 root thread 与入口 skill），受
  `max_session_tokens` 约束；已触顶时 `model` 步骤为 `skipped`，`errors["model"] = "session_token_limit"`。
- **预热期间投来的输入**：预热不是 turn，不消费输入。期间经 peer 投递等路径落到它名下的条目，
  在预热结束时写回 root history 与 store（R5）。
- **审计模式**：`Prewarm` 不在允许的 Op 之列，提交即被拒绝。

### 事件

| kind | data |
| --- | --- |
| `prewarm_started` | `steps`（列表） |
| `prewarm_completed` | `steps`（步骤 → 结果）、`errors`（失败步骤 → 说明）、`cancelled`、`duration_ms`、`usage`（无消耗为 `None`） |

两个事件的 `submission_id` 是 `Prewarm` 的 submission。都不进 LLM 视图。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| **R1** | 模型侧怎么预热由注入的实现决定；内核不假定任何 provider 的缓存机制 ✅ |
| **R2** | 不改 history 与缓存锚点。预热请求的前缀与首轮采样一致，是 provider 缓存能命中的前提 ✅ |
| **R3** | `prewarm_started` / `prewarm_completed`；指令解析与工作集变更沿用各自的事件 ✅ |
| **R4** | 每一步都接收取消 token；用户消息与 `CancelTurn` 都能取消 ✅ |
| **R5** | 不落任何持久化状态；进程重启后无需恢复 ✅ |

## 边界

- 不预取长期记忆：记忆的查询由用户输入决定，输入到来之前无从查起。
- 不预热子 skill、不预热分离派发的 child。
- 内核不决定何时预热（建会话后立刻、空闲一段时间后、缓存将过期时）；由业务提交 `Prewarm`。
