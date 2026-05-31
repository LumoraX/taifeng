# ADR 0005: Submission / EventMsg 双向消息总线

- 状态：Accepted
- 日期：2026-05-22

## 背景

agent 主循环的入口/出口设计有几种主流形态：

| 形态 | 代表 | 入口 | 出口 |
| --- | --- | --- | --- |
| Request-Response | LangChain, OpenAI Agents SDK | `agent.run(text)` | 同步返回 `RunResult` |
| Graph state machine | LangGraph | `graph.invoke(state)` | `astream_events()` |
| Actor + msg bus | codex, AutoGen v0.4 | `Submission { op }` 入队 | `Event { msg }` 出队 |
| Single-method stream | Pydantic AI | `agent.run_stream()` | AsyncIterator |

常见的 generator-based SSE 编排实现**入口是 HTTP request，出口是 SSE response**，二者强耦合——这是 Taifeng 想避免的反面教材。

## 决策

Taifeng 采用 **codex 风格的 Submission / EventMsg 双向消息总线**：

```python
class AgentEngine:
    submissions: asyncio.Queue[Submission]      # 业务侧 .submit(op) 入队
    events: asyncio.Queue[EventMsg]             # 业务侧 .subscribe() 出队
```

- 入口语义：`engine.submit(UserMessage("...")) -> submission_id`
- 出口语义：`async for event in engine.subscribe(): ...`
- 主循环：`AgentEngine.run(cancel)` 是单 actor，串行消费 submissions、并发产出 events

## 理由

### 解耦传输协议

业务侧的 SSE / WebSocket / gRPC 都是**传输层**，agent 引擎不应该知道。

```
HTTP SSE  ─┐
WebSocket ─┼─→ EventMsg ←── AgentEngine
gRPC      ─┘
```

业务侧的 controller 只做翻译：
```python
async def chat_sse(request):
    sub_id = await engine.submit(UserMessage(request.text))
    async for event in engine.subscribe_for(sub_id):
        yield to_sse_frame(event)
```

把 SSE 编码混在主循环里意味着**改协议要改主循环**——这是糟糕的耦合。

### 多前端共享后端

一个引擎常需同时服务多个前端（如富文本端与移动端），它们的 SSE 协议**有差异**：
- 一端期望 markdown / mermaid 块
- 另一端期望 content-block 协议

如果引擎层不解耦，每个前端要重复实现一遍 agent 逻辑。用 `EventMsg` 抽象后，每个前端写自己的翻译层即可。

### 取消语义清晰

```python
await engine.submit(CancelTurn(submission_id="xxx"))
```

取消是一个 `Op`，和 `UserMessage` 平级。主循环统一处理，不需要外部信号 + 状态查询的混合机制。

### 子 agent 派发自然

子 agent 派发可以通过同一个 bus：

```python
# 父 agent 决定派子 agent 时：
sub_id = await engine.submit(SpawnSubAgent(skill="xxx", parent=current_sub_id))
async for event in engine.subscribe_for(sub_id):
    # 子 agent 的事件流回父 agent
    if event.kind == "turn_complete":
        # 父 agent 拿到结果，继续自己的 turn
```

vs LangGraph 的 subgraph 嵌套 —— 后者要在图定义阶段就静态声明子图，运行时不能动态派生。

### 与 LLM provider 事件流分层

```
ResponseEvent  (LLM 粒度，9 类)        ← 引擎内部
    │
    ▼ TurnRunner 聚合
    │
EventMsg       (业务粒度，~15 类)      ← 业务订阅
    │
    ▼ 业务翻译
    │
SSE frame / WebSocket msg / gRPC stream
```

三层关注点：
- `ResponseEvent`：LLM 怎么吐字
- `EventMsg`：引擎语义事件（assistant_text / tool_call / turn_complete / compaction_attempted）
- 传输 frame：业务侧自定义

一种常见反模式是直接把 LLM 流粒度暴露给 SSE，**业务侧拿到十几种事件类型还要再筛**。Taifeng 通过两层抽象避免这个混乱。

## 后果

### 正面

- 引擎与传输协议解耦
- 多前端共享后端无重复
- 取消、子 agent、压缩这些操作都是平级 `Op`
- 单 actor 串行化 submission 处理，并发问题边界清晰

### 负面

- 业务侧入门门槛略高（需要理解 queue-based 异步模型）
- Debug 时事件流穿透多层，stack trace 不直观
- 性能：`asyncio.Queue` 比直接 callback 多一次调度

### 缓解措施

- 提供 `engine.submit_and_wait(op) -> RunResult` 同步包装，简单场景可用
- Telemetry 全链路注入 `submission_id`，调试时按 ID 串
- 性能：`asyncio.Queue` 在 IO-bound 场景调度开销 < 50μs，相比 LLM 调用毫秒级延迟可忽略

## 与 codex 实现的差异

| codex | Taifeng |
| --- | --- |
| Rust `tokio::mpsc` unbounded channel | Python `asyncio.Queue` bounded (默认 1024) |
| `Submission { id, op: Op::* }` | 同 |
| `Event { id, msg: EventMsg::* }` | 简化为 `EventMsg(submission_id, msg)` |
| `Op` 枚举 ~20 种 | 起步 5 种：UserMessage / CancelTurn / CompactNow / InjectSystemMessage / SetWorkdir |
| sticky routing via HTTP header | 同 + Python `contextvars` 注入 |

## 相关

- [架构：主循环](../architecture/agent-loop.md)
- [架构：对话持久化](../architecture/conversation.md)（ResponseItem vs EventMsg vs ResponseEvent 分层）
- ADR 0004（cache-aware 压缩）—— `CompactNow` 是 Op 的一种
