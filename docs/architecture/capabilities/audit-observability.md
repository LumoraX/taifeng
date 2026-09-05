# audit-observability Specification

## Purpose

审计可观测 **层1**：在不破坏 EventMsg「可丢 / 不阻塞主 actor / 吞异常」内核语义（R4）的前提下，补齐三件事——LLM request 全文留痕、事件序号自检（全局 + per-subscriber）、事件队列有界大容量 + 堆积告警。「可靠 fail-stop 审计真相源」是独立的层2 课题（留 ADR 0020），**不**搭 EventMsg 便车。

设计文档：`docs/superpowers/specs/2026-06-13-audit-observability-design.md`。关联 ADR 0005（Submission/EventMsg 双总线）、ADR 0017（立项四规则）；扩展 [telemetry-otel](telemetry-otel.md)。

## Requirements

### Requirement: 事件全局序号 seq

系统 SHALL 在每条 `EventMsg` 上提供单调递增的全局序号 `seq: int`（默认 `0`，旧序列化数据冷读兼容）。

- `seq` SHALL 在 `engine._emit` 入口同步分配（`ev.seq = self._seq; self._seq += 1`），分配点全程无 `await` 让出点 → 并发多 turn/spawn 下原子、不重不漏。
- `seq` 的作用域 SHALL 是单 engine（= 单 session）；`EnginePool` 为每个 `session_id` 新建一个 `AgentEngine`，`_seq` 是 engine 实例字段。
- 落库主键 SHALL 用 `(session_id, seq)` 复合键达成全局唯一；`session_id` **不**盖在事件上，由订阅方按所属 engine 提供（见 `engine.session_id` 只读属性）。
- 全局 `seq` 的连续性自检 SHALL 仅对 `subscribe_all`（全量 firehose）成立；过滤订阅（`subscribe(submission_id)`）只收子集、`seq` 天然跳号（=过滤非丢弃），**不得**据此判 drop。

#### Scenario: seq 在 emit 入口单调分配
- **WHEN** 连续 `engine._emit(e0); engine._emit(e1); engine._emit(e2)`
- **THEN** `(e0.seq, e1.seq, e2.seq)` SHALL 等于 `(0, 1, 2)`

#### Scenario: session_id 只读属性
- **WHEN** 构造 `AgentEngine(..., session_id="sess-1")`
- **THEN** `engine.session_id` SHALL 返回 `"sess-1"`
- **WHEN** 未显式传 `session_id`
- **THEN** `engine.session_id` SHALL 退回 `thread_id`

### Requirement: 晚到订阅者终态补投

系统 SHALL 记住每个 submission 的最后一条终结事件，并在过滤订阅命中时立即补投。

- 终结 kind SHALL 为 `turn_completed` / `turn_failed` / `turn_suspended` 三者之一，与过滤订阅的收尾判定
  **同集合**（单一真相，不得各写一份字面量）。
- `subscribe_envelopes(submission_id)` SHALL 在登记订阅**之前**查询终态记账；命中 SHALL 补投**原事件**
  （保持其全局 `seq`）后收尾，且 SHALL NOT 占用 per-submission 订阅位。
- 补投事件的 `delivery_seq` SHALL 从 0 起（新订阅的独立簿记）。
- 记账 SHALL 有界（`terminal_replay_size`，默认 256，FIFO 淘汰最老）；`<=0` SHALL 关闭补投。
  被淘汰后 SHALL 退化为等待，SHALL NOT 无界缓存。
- **未命中记账的 submission SHALL 维持等待**（`subscribe` 早于 `submit` 是合法且推荐的用法）。
- 同一 submission 多次终结 SHALL 以最后一条为准。

### Requirement: per-subscriber 投递序号 delivery_seq

系统 SHALL 通过 `DeliveredEvent { event: EventMsg, delivery_seq: int }` 信封向订阅者暴露 per-subscriber 投递序号。

- `delivery_seq` SHALL 对每个订阅各自从 `0` 起连续。
- 投递序号 SHALL 在每次入队**尝试**时分配；队列满被丢弃（`QueueFull`）时该序号仍被消耗（烧号），使订阅者收到的 `delivery_seq` 跳号 = **它自己**漏了事件（与全局 `seq` 跳号互不混淆）。
- 系统 SHALL 提供 `subscribe_all_envelopes()` / `subscribe_envelopes(submission_id)` 产出 `DeliveredEvent`；`subscribe_all()` / `subscribe()` 保持向后兼容产出裸 `EventMsg`（内部委托信封版、解包 `.event`）。

#### Scenario: 队列满时 delivery_seq 烧号可自检
- **GIVEN** 一个容量 2、不消费的 firehose 订阅
- **WHEN** emit 3 条事件
- **THEN** 队列内 2 条的 `delivery_seq` SHALL 为 `[0, 1]`
- **AND** 订阅者下一个投递序号 SHALL 为 `3`（第 3 条烧号）
- **AND** `engine.events_dropped` SHALL 为 `1`

### Requirement: 事件队列有界大容量 + 高/低水位告警

系统 SHALL 默认采用**有界大容量**事件队列（`event_queue_size` 默认 `65536`），而非无界——无界 + 慢/缺席消费者会 OOM 拖垮 engine，比有界丢弃的优雅降级更糟。

- `event_queue_size` SHALL 可配；`<=0` 表示无界（opt-in，业务自负 OOM 风险）。
- 系统 SHALL 在有界队列 `qsize()` 上穿高水位（默认 `event_high_water_ratio=0.75`）时打一条 `logger.warning`；回落到低水位（默认 `event_low_water_ratio=0.5`）以下才重新武装（迟滞）；告警另受 `event_warn_cooldown_sec`（默认 5s）限频。
- 堆积告警 SHALL 走 logger 而非 emit 事件（告警事件本身也进所有队列，会自我放大成风暴）。
- 队列满 SHALL 计数 `events_dropped` 并 `logger.warning`，**不**阻塞主 actor（`put_nowait`，R4）。

#### Scenario: 默认有界大容量
- **WHEN** 不传 `event_queue_size` 构造 engine
- **THEN** `engine._event_queue_size` SHALL 为 `65536`

#### Scenario: 高水位告警迟滞
- **GIVEN** 容量 4（高水位 3）、不消费的订阅
- **WHEN** 连续 emit 4 条事件
- **THEN** SHALL 恰好打出 1 条 high-water `logger.warning`（迟滞，不每条都刷）

#### Scenario: 无界队列不告警
- **WHEN** `event_queue_size=0`（无界）且 emit 大量事件
- **THEN** SHALL 不打任何 high-water 告警（无容量百分比可言）

### Requirement: LLM request 全文留痕

系统 SHALL 提供 `enable_request_capture: bool`（默认 `False`，零泄漏面）开关；开启后在每次实际构建发送的 request 前 emit 一条 `LlmRequestRecorded` 事件（`data = ApiRequest.model_dump()`）。

- 注入点 SHALL 在 `turn.py` `build_api_request` 之后、发送 provider 之前（即便 provider 超时/失败，request 仍留痕）。
- retry / mid-turn 压缩重建 request 走新一轮构建 → SHALL 各 emit 一条（「每次实发各一条」），留痕的是真正发出的那一版。
- `LlmRequestRecorded` 含完整 prompt + conversation（敏感）：`OtelTelemetrySink` SHALL 按 kind 整条跳过、不转 OTel；可靠落盘 / 脱敏 / 访问控制 / 保留期 SHALL 全归业务消费者（内核只留痕、不治理）。

#### Scenario: 默认不留痕
- **WHEN** `enable_request_capture=False` 跑一次 turn
- **THEN** SHALL 不出现 `llm_request_recorded` 事件

#### Scenario: 开启后含全文
- **WHEN** `enable_request_capture=True` 跑一次 turn
- **THEN** SHALL 至少出现一条 `llm_request_recorded`
- **AND** 其 `data` SHALL 含 `model` 与 `messages`（ApiRequest 全文）

#### Scenario: OtelSink 不外发 request 正文
- **WHEN** `OtelTelemetrySink.handle` 收到 `llm_request_recorded`
- **THEN** SHALL 整条跳过——既不作为 generic span-event 出现，也不带任何 request 正文

## Data Contract

```python
# src/taifeng/loop/event.py
class EventMsg(BaseModel):
    submission_id: str
    msg: Msg
    timestamp: datetime
    seq: int = 0                       # 全局总线序号（engine._emit 入口分配）

class LlmRequestRecorded(_Msg):
    kind: Literal["llm_request_recorded"]
    # data = ApiRequest.model_dump()

# src/taifeng/loop/engine.py
@dataclass(frozen=True)
class DeliveredEvent:
    event: EventMsg
    delivery_seq: int                 # per-subscriber 投递序号（含丢弃烧号）
```

## R1–R5 影响

- **R1 业务零侵入**：`session_id` 是内核概念（非 tenant/audience/领域名词），不进事件、由 sink 边界提供。
- **R2 cache 友好**：不触压缩路径，无影响。
- **R3 可观测**：新增 `llm_request_recorded` 事件 + `seq`/`delivery_seq` 自检，强化可观测。
- **R4 可取消 / 不阻塞**：队列有界但仍 `put_nowait` 永不阻塞主 actor；丢弃计数 + 自检，绝不 OOM。
- **R5 可 resume**：`seq` 带默认值冷读兼容；不改 resume 路径。

## 层2 边界（不在本契约）

可靠 fail-stop 审计真相源（同步 / 事务 / resume 级可靠的 `MessageWriter` 那一类可靠 append 线）是独立课题，留 ADR 0020，**不**改造 EventMsg emit 路径。
