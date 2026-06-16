# skill-outcome-record Specification

## Purpose

Skill 执行战绩沉淀 —— 认知回路 ⑦「沉淀相位」的地基。

每次 `call_skill` 派发的子 skill 达到**终态**（success / failure / abandoned）时，内核在其对应子 thread 上追加一条 `skill_outcome` 旁路记账 item，并 emit 一条 `skill_outcome_recorded` 事件。`suspended`（挂起等待）态**不产生记录**——挂起不是终态。

设计原则：**只采集、不决策**。v1 产出「这一次 skill 执行干成没有」的结构化记录，供后续相位（fitness 计算 / 提拔 / 逐出 / 隔离）消费。v1 **不做**任何检索、置信度策略、提拔动作。

关联设计文档：`docs/superpowers/specs/2026-06-16-skill-capability-acquisition-loop-design.md`（§5 战绩沉淀）。

## Requirements

### Requirement: 终态触发、挂起不记

系统 SHALL 在子 skill `_spawn_sub_runner` 完成后**仅当**其 `end_reason` 属于终态（`success=True` 或 `success=False`）时，才产生战绩记录。

- 子 skill `end_reason == "suspended"` SHALL **不**产生 `skill_outcome` 记录——挂起不是终态，记录应在最终结局（resume 后续跑完成或 abort）时产生。
- 一次 call_skill 生命周期中 SHALL 恰好产生**零条或一条**战绩记录：零条（挂起路径）或一条（终态路径）。

#### Scenario: 成功子 skill 产生 success 记录
- **GIVEN** entry skill 通过 `call_skill` 派发一个子 skill
- **WHEN** 子 skill 正常完成（`success=True, end_reason="completed"`）
- **THEN** 子 thread 的 JSONL 追加一条 `kind="skill_outcome"` item
- **AND** `SkillOutcomeRecorded` 事件 emit，`data.outcome == "success"`

#### Scenario: 挂起子 skill 不记录
- **GIVEN** entry skill 通过 `call_skill` 派发一个子 skill
- **WHEN** 子 skill 以 `end_reason="suspended"` 挂起提前返回
- **THEN** JSONL **不**追加 `skill_outcome` item
- **AND** **不** emit `skill_outcome_recorded` 事件

#### Scenario: 护栏触发的 abandoned
- **GIVEN** 子 skill 因 `max_iterations` / `denial_circuit_open` / `doom_loop_circuit_open` / `resource_limit_exceeded` / `cancelled` 被截断
- **THEN** `kind="skill_outcome"` item 产生，`outcome == "abandoned"`

#### Scenario: 错误 / 未知终态归 failure
- **GIVEN** 子 skill 以 `success=False` 且 `end_reason` 不在 abandoned 集（如 `end_reason="error"` 或 LLMError 硬失败）结束
- **THEN** `kind="skill_outcome"` item 产生，`outcome == "failure"`

### Requirement: 长相与战绩分离不变量

系统 SHALL 保证 `selection_confidence`（来自发现 / 评估相位的长相分）与 `outcome`（战绩）**独立存储**，且 **v1 中任何内核路径禁止把 `selection_confidence` 直接用于提拔决策**。

- v1 中 `selection_origin` SHALL 恒为 `"whitelist"`（全部 skill 来自白名单注册）。
- v1 中 `selection_confidence` SHALL 恒为 `None`（发现相位未实现，无分数可填）。
- 业务侧可通过注入自定义 `OutcomeJudge` 注入业务信号，但协议字段的物理分离 SHALL 不变。

### Requirement: OutcomeJudge 协议与 StructuralOutcomeJudge

系统 SHALL 提供 `OutcomeJudge`（`typing.Protocol`，`runtime_checkable`）供业务侧注入自定义裁决逻辑。

```python
@runtime_checkable
class OutcomeJudge(Protocol):
    def judge(self, ctx: SkillExecutionContext) -> OutcomeVerdict: ...
```

内核默认实现 `StructuralOutcomeJudge` 只用可见硬信号（业务无关）：

| 条件 | 战绩 |
| --- | --- |
| `success=True` | `success` |
| `success=False` 且 `end_reason ∈ {"cancelled","max_iterations","resource_limit_exceeded","denial_circuit_open","doom_loop_circuit_open"}` | `abandoned` |
| 其余非成功（含 `error` / 未知 `end_reason`） | `failure` |

注：`suspended` 不会到这里——挂起在 `_spawn_sub_runner` 内提前返回，`judge` 不被调用。

#### Scenario: 业务注入自定义 OutcomeJudge
- **WHEN** `EnginePool.create(outcome_judge=MyJudge())` / `AgentEngine(..., outcome_judge=MyJudge())` / `TurnRunner(..., outcome_judge=MyJudge())`
- **THEN** 子 skill 终态时调用 `MyJudge.judge(ctx)` 产出 `OutcomeVerdict`，而非 `StructuralOutcomeJudge`
- **AND** `MyJudge` SHALL 继承（透传）到子 TurnRunner（call_skill / spawn），不在父层截断（R1 注入缝）

#### Scenario: isinstance 检测
- **WHEN** `isinstance(StructuralOutcomeJudge(), OutcomeJudge)`
- **THEN** SHALL 返回 `True`

## Data Contract

### SkillExecutionRecord（`src/taifeng/skill/outcome.py`）

```python
@dataclass(frozen=True)
class SkillExecutionRecord:
    skill_id:               str               # 执行的 skill ID
    call_id:                str               # 本次调用 call_id（全 session 唯一，与 function_call item 配对）
    parent_call_id:         str | None        # 父调用的 call_id（根层为 None）
    depth:                  int               # 调用深度（根层=0，每层 call_skill +1）
    source:                 SkillSource       # "atomic" | "composite" | "orchestration"
    trust_tier:             str | None        # 信任层级（v1 恒 None；预留给发现相位）
    selection_origin:       SelectionOrigin   # "whitelist"（v1 恒）| "discovered"（发现相位填）
    selection_confidence:   float | None      # 长相分（v1 恒 None；发现 / 评估相位填；禁止喂提拔）
    outcome:                OutcomeStatus     # "success" | "failure" | "abandoned"
    outcome_signal_source:  OutcomeSignalSource  # "structural"（默认）| "business"（业务注入时）
    end_reason:             str               # 原始 end_reason（completed / error / cancelled / 护栏类型…）
    error_detail:           str | None        # 错误摘要（failure 时由 judge 或业务填；success/abandoned 为 None）
    cost_tokens:            int               # 本次 skill 执行消耗的 token 数
    cost_duration_ms:       int               # 执行耗时（ms）
    cost_iterations:        int               # 迭代轮数（LLM 采样次数）
    ts_unix:                int               # 记录写入时间戳（Unix 秒）
```

**`as_payload() -> dict`**：转 JSON-safe dict，作为 `ResponseItem.payload` 落 JSONL 并填入 `SkillOutcomeRecorded.data`。

### SkillExecutionContext（裁决输入，仅传给 OutcomeJudge）

```python
@dataclass(frozen=True)
class SkillExecutionContext:
    success:    bool      # TurnOutcome.success
    end_reason: str       # TurnOutcome.end_reason
    error:      str | None  # 异常字符串摘要（有则非 None）
```

### OutcomeVerdict（裁决输出）

```python
@dataclass(frozen=True)
class OutcomeVerdict:
    status:        OutcomeStatus       # "success" | "failure" | "abandoned"
    reason:        str                 # 裁决原因（如 "completed:completed" / "abandoned:cancelled"）
    signal_source: OutcomeSignalSource # "structural" | "business"
```

### SkillOutcomeRecorded 事件（`src/taifeng/loop/event.py`）

```python
class SkillOutcomeRecorded(_Msg):
    kind: Literal["skill_outcome_recorded"] = "skill_outcome_recorded"
    # data = SkillExecutionRecord.as_payload()
    # 包含全部 SkillExecutionRecord 字段
```

### skill_outcome ResponseItem（`src/taifeng/conversation/models.py`）

```python
ItemKind = Literal[
    ...,
    "skill_outcome",   # 旁路记账；不进 LLM 消息序列；`build_api_request` 跳过此 kind
]
```

构造器：

```python
def skill_outcome_item(payload: dict[str, Any], *, thread_id: str) -> ResponseItem:
    """旁路落 JSONL，不进 LLM 视图。payload = SkillExecutionRecord.as_payload()"""
```

## 行为契约

### 触发点（`src/taifeng/loop/turn.py::_spawn_sub_runner`）

```
call_skill 工具触发 → _spawn_sub_runner
  ├─ 子 TurnRunner.run_turn(子 thread)
  │     ...
  │     end_reason == "suspended"
  │       → 早期 return（不调 judge，不产生记录）
  │     end_reason != "suspended"（终态）
  │       → OutcomeJudge.judge(SkillExecutionContext(success, end_reason, error))
  │       → 构造 SkillExecutionRecord（含 cost_* / ts_unix）
  │       → store.append([skill_outcome_item(record.as_payload())])   # 落子 thread JSONL
  │       → engine.emit(SkillOutcomeRecorded(data=record.as_payload()))   # R3 事件
  │
  └─ 父 turn 继续 / _spawn_sub_runner 返回 ToolResult
```

### 旁路语义

`skill_outcome` item **永远不进入 LLM 消息序列**：

- `build_api_request` 在 history → API messages 转换时跳过 `kind="skill_outcome"` item。
- `reconstruct_logical_history` 把 `skill_outcome` 按「直接追加」处理（不参与 compacted / rewind 折叠）。
- `load_history` 正常载入（含 `skill_outcome`），供后续相位异步读取战绩数据。

### 子 TurnRunner 继承（R1 注入缝）

`outcome_judge` 通过构造参数从 `EnginePool.create` → `AgentEngine` → 每个 `TurnRunner` 透传；子 TurnRunner（call_skill 嵌套 / spawn detached）继承父 runner 的 `outcome_judge`，业务注入一次即全链生效。

## R1–R5 影响

| 红线 | 影响 |
| --- | --- |
| **R1 业务零侵入** | `OutcomeJudge` Protocol 是唯一业务注入缝；`src/` 内无 tenant / 领域名词；`StructuralOutcomeJudge` 开箱可用，不注入也能跑 ✅ |
| **R2 Cache 友好** | `skill_outcome` item 为旁路，不进 LLM 消息视图，不影响 prompt 构建和 cache anchor；R2 中性 |
| **R3 可观测** | `SkillOutcomeRecorded` 事件（`kind="skill_outcome_recorded"`）含全量 record 字段，供 TelemetrySink 订阅 ✅ |
| **R4 可取消** | `cancelled` 端原因 → `abandoned` 战绩，正确采集取消终态；不新增阻塞点 ✅ |
| **R5 可 resume** | `skill_outcome` item 以 `store.append` 追加写 JSONL，满足 append-only；`load_history` 正常回载；不依赖 engine 内存状态 ✅ |

## 边界与 v1 明确不做的事

以下是 v1 的**显式边界**，后续相位才实现：

| 不做 | 原因 |
| --- | --- |
| 战绩检索 / 向量化 | 存储由业务侧 `IndexHook` / `MessageWriter` 决定，内核不越界 |
| fitness 计算 / 提拔 / 逐出 | 认知回路 ⑦ 的上层相位，不在本契约范围 |
| selection_confidence 策略化 | 发现相位（⑥）尚未实现；v1 恒 None |
| 跨 session 聚合统计 | 外部 DB / 分析层的职责，内核不承载 |
| spawn detached 子 skill 的战绩记录 | detached spawn 子 thread 为独立 TurnRunner，其终态处理路径与 call_skill 不同；v1 仅覆盖 call_skill 路径 |
