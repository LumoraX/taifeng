# ADR 0037：旧 provider 路径对齐主路径不变量；重试装饰器与 attempt 契约互斥

- 状态：Accepted
- 日期：2026-09-07
- Amends：ADR 0026（provider native 契约）、ADR 0030 / 0033 / 0034（主路径终止与记账加固）
- 关联：[llm-provider-native 契约](../architecture/capabilities/llm-provider-native.md)；[llm-client 活文档](../architecture/llm-client.md)；[tool-builtins-extended](../architecture/capabilities/tool-builtins-extended.md)；[session-journal-core](../architecture/capabilities/session-journal-core.md)；openspec change `wave3-provider-path-alignment`

## 背景

2026-09-03 全系统审查的 Wave 3。`openai_compat` 与两条 Responses 路径在前几波里被逐条加固，而 gemini / anthropic / litellm 三条旧 provider 路径、以及几处主路径之外的旧代码，从未跟上同一批不变量。在 main（0bc10a5）逐处核对后确认七个真实缺陷（回归用例 29 条，改动前全红）：

1. **旧 provider 流末不判终止真相**：三家都没有 `terminal_seen` 与异常 finish_reason 保护。连接被中途掐断、或被安全策略拦截返回空 content，都会被伪造成「成功的空回复」`completed`，违反「LLM 侧不存在空回复，有即异常」不变量。
2. **旧 provider 漏接 `RemoteProtocolError`**：它属 `ProtocolError` ≠ `NetworkError`，gemini / anthropic 只 catch 后者 → 裸逃 → 落 `unknown` 硬失败，既不重试也不挂起恢复。`openai_compat` 早已放宽到 `TransportError`。
3. **`completed` 丢弃终止原因**：只有 `end_turn: bool`，`length`（截断）与 `stop`（自然结束）在内核眼里完全一样。
4. **gemini `functionResponse.name` 填的是 call_id**：Gemini 要求函数名，工具结果与函数声明对不上，多轮工具链在该路径上语义损坏。
5. **`retry_async` 是死代码**：全仓零调用点，而 `turn.py` 注释写着「retry 已由 provider 内 retry_async 兜底」。实测后果：wave2c 台账连跑 8 轮，每轮 3–8 个场景因**一次** `provider_internal ServerError` 直接转 SYSTEM_RETRY 挂起，而该 kind 恰在 `retryable_kinds` 里。
6. **`shell_exec` / `run_in_background` 默认继承整个 `os.environ`**：子进程可读 API key 等全部凭据。同仓 `ShellScriptExecutor` 早有白名单，两个内置工具没复用。
7. **audit journal 拒收合法 payload 键**：item payload 模型 `extra="forbid"` 却缺 `extra_content` / `attachments`，一条带图片的工具结果直接冻结 session。

外加压缩侧一处：`surgical_trim` / `offload` 对孤儿 output 一律跳过，而两者就地改写 payload、不删条目，跳过的真实代价是压不动最大的那条。

## 决策

### 1. `stop_reason` 原样透传，不跨家归一

`completed` 增 `stop_reason: str | None`，各 provider 填其**原生**字符串（anthropic `stop_reason` / gemini `finishReason` / openai 与 litellm `finish_reason` / Responses `status`）。各家取值语义不等价，建映射表必然丢信息，且没有任何消费点需要跨家统一判断——归一交业务侧。`end_turn: bool` 语义与默认值不变，零回归。turn 透出 `turn_completed.data.stop_reason`。

### 2. 终止真相规则统一、实现各自落地

不抽公共基类（三家 SSE 形态差异大，强抽会把三个简单判断变成难读的模板），而是共享一份**规则**与一个分类器 `_shared.classify_abnormal_finish()`：
- `terminal_seen`：gemini = 见过任一 `finishReason`；anthropic = 见过 `message_stop`；litellm = 见过任一 `finish_reason`。未见 → `InvalidResponseError`。
- 异常终止原因 **且本次调用零内容产出** → emit `error` 事件（与 HTTP 错误路径一致）+ 抛既有分类异常。已有产出说明模型确实干了活，不作废。
- `except httpx.TransportError`（含 `RemoteProtocolError`）→ `TransientNetworkError`；litellm 在其分类器补同义关键字。

### 3. 重试做成外层装饰器，与 attempt 契约刻意互斥

`OneNetworkAttemptModelClient` 契约是「每次 `stream` 恰有一个网络 attempt」，strict audit 的 checkpoint lineage 依赖它——**这正是 `retry_async` 一直没接线的真实原因**。故新增 `RetryingModelClient`（`llm/retrying.py`）：

- **零产出才重试**：任一 `text_delta` / `reasoning_delta` / `tool_call_delta` / `tool_call_done` / `structured_output` / `normalized_output` 已 yield 即置位；置位后失败直接抛，绝不重发（否则调用方收到重复文本与重复 tool call）。这是本装饰器的正确性基石。
- 元信息事件（`created` / `server_model` / `rate_limits`）只投递一次，重试不重发。
- 退避走 `wait_cancelled` 竞速而非裸 `sleep`——限流 hint 可达数十秒，`CancelTurn` 必须立即生效（R4）。
- **刻意不声明** `OneNetworkAttemptModelClient`，strict audit 会如实拒绝它。互斥是设计约束，不是缺陷。
- `retry._compute_delay` 提升为公开 `compute_backoff_delay`，两条重试路径共用同一退避算法。
- `examples/_provider_bootstrap.build_model_client(retry=True)` 默认包一层。

### 4. 子进程 env 白名单是默认，不是开关

`env=None` 继承全环境是「默认不安全」。改默认而非加开关：安全默认必须是默认。白名单实现提到 `tool/subprocess_env.py`，三处消费方共用（避免同一份安全判据出现第二个副本各自漂移）。

### 5. journal payload 覆盖内核实际写入的键

`extra="forbid"` 是对的（未知键必须暴露），错的是模型没跟上写入方。补 `extra_content` / `attachments` 两个可选键，缺省不写（逐键形状与既有持久化数据一致）。

### 6. 孤儿 output 可降级处理

两个策略都就地改写 payload、不删条目，故处理孤儿不可能产生新的配对孤儿（G1b 回滚不会被触发）。`offload` 去掉配对要求；`surgical_trim` 只在 glob 为**默认全允许**（判定与工具名无关）时剪孤儿，配了具体 glob 仍跳过——「无法判定即不动」的原则保留在真正需要它的地方。

## 替代方案

- **把重试放进 provider 内部**：直接违反 `OneNetworkAttemptModelClient` 契约，破坏 audit lineage；否决。
- **`RetryingModelClient` 声明 attempt 契约并只重试「流未开始」的失败**：判据比「零产出」更窄（已发 `created` 就不能重试），且要在装饰器里伪造 attempt 边界骗过 audit；否决。
- **矩阵降级（承认 retry 没接线）**：能力表如实了，但 wave2c 实测的挂起风暴仍在；这是可修的真缺陷，不该降级；否决。
- **`stop_reason` 归一为统一枚举**：各家语义不等价，归一丢信息且无消费方需要；否决。
- **env 白名单做成 opt-in 开关**：安全默认必须是默认；否决。
- **孤儿 output 一律可剪（含配置了 glob 时）**：会在业务明确限定了可剪工具面时剪掉它没授权的条目；否决。

## 后果

- **BREAKING（行为）**：① `shell_exec` / `run_in_background` 的子进程不再看到宿主完整环境变量，需要更多变量的业务显式传 `env=`；② 三家旧 provider 此前被静默吞成「空回复成功」的调用现在会显式失败——这正是目的，但会让原本"看起来成功"的调用暴露为错误；③ `build_model_client` 默认返回 `RetryingModelClient` 包装（需要 native 类型或 strict audit 的调用方传 `retry=False`）。
- `completed` / `turn_completed` 新增 `stop_reason` 键 → sim 的事件形状随之变化，金样已随本波真实回归重录。
- `RetryingModelClient` 与 strict audit 互斥，文档与契约均已明示。
- Wave 3 未覆盖：provider 端把历史 fc 参数回放为 `{}` 的容错（anthropic / gemini `_to_*_messages`）留待后续；engine.py / turn.py 拆分属 Wave 4。
