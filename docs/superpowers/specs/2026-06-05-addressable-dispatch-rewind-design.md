# 设计:派发可寻址 + Turn 内 Rewind(自治链中间步重试)

- 日期:2026-06-05
- 状态:待 review
- 适用红线:R1(业务零侵入)/ R2(cache 友好)/ R3(可观测)/ R4(可取消)/ R5(可 resume)—— 本能力改 dispatch/turn 模型,按 CLAUDE.md 必须逐条声明影响(见 §7)

## 1. 问题陈述

自治链「一键跑完」= 用户**一条** `UserMessage` → 根 entry skill 的 LLM 在**同一个 root turn 内**连续 `call_skill(B)`、`call_skill(C)`… 多次 → **一个** `turn_completed`。多个步骤是这一个 turn 里的**嵌套 tool 调用**,中间**没有 user-message 边界**。

由此带来两个现状缺口:

1. **无法外科手术式重跑中间某一步**。现有 [`ThreadRollback`](../../../src/taifeng/loop/submission.py) 的回滚粒度是「一轮 = user_message 到下一个 user_message」,比 `call_skill` 派发边界**粗一级**。自治链整条只有一轮,ThreadRollback 要么不退、要么把整条链全废。

2. **README 的「entry:true 纯加法」是伪命题**。`examples/step_pipeline/README.md` 声称给子 skill 加 `entry: true` 后「既能被自治链 `call_skill` 派发,又能被业务编排单独拉起」是**纯加法**。但 [`dispatch.py:175`](../../../src/taifeng/skill/dispatch.py) 在 `target.entry` 为真时直接 `reject("cannot_call_entry_skill")`,且能力契约 [`skill-dispatch.md`](../../architecture/capabilities/skill-dispatch.md) 明文「跨 entry skill 派发必须开新 session」。即:`entry: true` 这个动作**本身就掐断了 call_skill 路径**——采用 step_pipeline 等于**替换**自治编排,不是叠加。step_pipeline 的 demo 因为根本不走 `call_skill`,所以没踩到这条。

## 2. 目标与非目标

**目标**

- 让自治链(任意用 `call_skill` 的 composite skill)**既能一口气自动跑完,又能直接重试其中任意一次派发**,且重试发生在**同一次自治 run 内**。
- 通用内核原语,落 `src/taifeng/`,**零业务概念**(守 R1);不只服务医疗,任何自治链白捡。
- 子 skill 全程保持 `entry: false`——**绕开 entry/call_skill 矛盾**,不依赖把子 skill 变 entry。

**非目标(本期不做)**

- 不做确定性「录后重放」模式(原方案 B);只在 §8 留作未来可选 policy 旋钮。
- 不改 `entry: true` 的语义,不放开「entry skill 可作为 call_skill target」的约束(那是另一条 ADR)。
- 不改 step_pipeline example 的业务层编排器(pipeline.py 仍是另一种范式,保留)。
- 不做跨 turn / 跨 session 的 rewind(只在**活着的 root turn**内,或可 resume 的挂起态)。

## 3. 核心设计

三个支点:**派发边界 checkpoint** → **新 Op `RewindDispatch`** → **主动重推根 turn**。

### 3.1 派发边界 checkpoint(`loop/turn.py`)

root turn 内每次 `call_skill` 派发落地时(即 [turn.py:785-798](../../../src/taifeng/loop/turn.py) 把 `function_call` + `function_call_output` 追加进 `history_buffer` 处),记一条轻量 checkpoint:

```python
@dataclass(frozen=True)
class DispatchCheckpoint:
    """一次 call_skill 派发的可回退锚点(turn 内,append-only 不破)。"""
    dispatch_index: int        # 本 turn 内第几次 call_skill 派发(从 0 起)
    call_id: str               # 对应 function_call 的 call_id
    target_skill_id: str       # 被派发的子 skill
    history_len_before: int    # 派发前 history_buffer 长度 = rewind 截断点
    cache_anchor_before: int   # 派发前 cache_anchor_index(回退时还原)
    args_digest: str           # 原始 args 摘要(供 UI 展示/审计,非重放依赖)
```

- checkpoint 存在 root `TurnRunner` 上的 `dispatch_checkpoints: list[DispatchCheckpoint]`,turn 结束随状态回写 engine(与 `history_buffer` / `cache_anchor_index` 同路径,见 engine.py:745-747)。
- **append-only 不破(R5)**:checkpoint 只记「截断点的下标」,不物理删 history;rewind 时按下标切片得到新 history,旧 thread 仍可落 store。
- checkpoint 是 turn 内派发的**侧录**,不进 LLM 上下文,不影响 prompt 指纹。

### 3.2 新 Op `RewindDispatch`(`loop/submission.py`)

```python
class RewindDispatch(BaseModel):
    """回退到 root turn 内第 k 次 call_skill 派发之前,重跑该派发并续推。

    语义(默认 = re-reason):
        1. 把 history 截回 checkpoint[k].history_len_before
        2. cache_anchor 回退到 checkpoint[k].cache_anchor_before(cache 从 k 起失效,expected)
        3. 重跑子 skill k(若给 new_args 则用新 args,否则用原 args)
        4. 根 LLM 从该点继续采样,**自由重新决定下游调哪几步**
    """
    kind: Literal["rewind_dispatch"] = "rewind_dispatch"
    dispatch_index: int                 # 要重跑的派发序号 k
    new_args: dict[str, Any] | None = None   # None=原样重跑;非 None=换输入重跑
```

### 3.3 主动重推根 turn(`loop/engine.py`)

新增 `_handle_rewind_dispatch(submission_id, op)`,**结构上是 `_handle_rollback` 的截断三件套 + `_handle_resume` 的主动重推**:

```
async def _handle_rewind_dispatch(sub_id, op):
    1. 取 checkpoint = self._dispatch_checkpoints[op.dispatch_index]
       - 越界 / 无活跃 turn / 该 turn 已不可 rewind → 拒绝并发 EventMsg(见 §6)
    2. async with self._lock:
       - self._history = self._history[:checkpoint.history_len_before]   # 截断(同 rollback)
       - self._cache_anchor_index = min(anchor, checkpoint.cache_anchor_before)  # 回退 anchor
       - 丢弃 dispatch_index >= k 的 checkpoint(它们已失效)
    3. 落一条 system_injection marker:"[rewind] to dispatch k (skill X)"(同 rollback marker 范式)
    4. 重建一个 root TurnRunner,history_buffer=截断后的 history,
       先以 new_args(或原 args)重跑子 skill k(走 run_sub_skill),
       再让根 LLM 从该点继续采样到 turn 终结(完成/再挂起/失败)。
    5. 全程透传同一 CancellationToken(R4),cancel.child() 派生子 skill k 的 token。
```

**关键差异**:`ThreadRollback` 是**被动**(截断后等下一条 user message);`RewindDispatch` 是**主动**(截断后立刻重跑子 skill + 续推根 turn),复用 `_handle_resume` 的重推骨架。

## 4. 数据流(重试第 k 步)

```
业务侧 submit(RewindDispatch(dispatch_index=k, new_args?))
  → AgentEngine 入队
  → _handle_rewind_dispatch
     ├─ 取 checkpoint[k];校验(活跃 turn / k 在界内)
     ├─ history 截回 checkpoint[k].history_len_before(锁内)
     ├─ cache_anchor 回退 → emit cache 失效(expected,R2)
     ├─ emit EventMsg.turn_rewound { dispatch_index=k, target_skill=X }(R3)
     ├─ 重建 root TurnRunner(截断后 history)
     │    ├─ run_sub_skill(X, new_args or 原 args) → 子 turn(cancel.child(),R4)
     │    └─ 根 LLM 续采样 → 自由重决下游 call_skill(可能与首跑不同)
     └─ turn 终结 → MessageStore.append → EventMsg.TurnComplete
```

## 5. 模块改动清单

| 模块 | 改动 | 行数预算 |
| --- | --- | --- |
| `loop/turn.py` | `DispatchCheckpoint` dataclass;派发处记 checkpoint;runner 暴露 `dispatch_checkpoints` | ~40 |
| `loop/submission.py` | `RewindDispatch` Op + 并入 `Op` union | ~25 |
| `loop/engine.py` | `_handle_rewind_dispatch`;dispatch 分支;状态回写带 checkpoints | ~90 |
| `loop/event.py` | `DispatchCheckpointRecorded` / `TurnRewound` EventMsg(R3) | ~30 |
| `context/` | rewind 返回 `CompressionResult{cache_invalidated, anchor_preserved_until}`(R2) | ~15 |
| `docs/architecture/capabilities/skill-dispatch.md` | 新 Requirement「派发可寻址 + RewindDispatch」 | 契约 |
| `docs/architecture/agent-loop.md` | 活文档同步(turn 内 checkpoint / 新 Op) | 活文档 |
| `docs/decisions/NNNN-addressable-dispatch-rewind.md` | ADR:为什么细到派发粒度、为什么默认 re-reason、为什么不放开 entry 约束 | ADR |
| `examples/step_pipeline/README.md` | 修正「纯加法」伪命题,指向本能力 | 文档 |
| `tests/test_dispatch_rewind.py` | 新测试(见 §9) | 新文件 |

## 6. 错误与边界(禁止 silent fallback)

| 场景 | 行为 |
| --- | --- |
| `dispatch_index` 越界 / 无 checkpoint | 拒绝,emit `EventMsg` rewind_rejected(reason=`unknown_checkpoint`);不静默 no-op |
| 当前无活跃 root turn(turn 已结束且非可 resume 态) | 拒绝,reason=`no_rewindable_turn` |
| 该 turn 处于挂起(HITL)态 | v1 拒绝,reason=`turn_suspended`(rewind 挂起态留待后续);明确报错不猜 |
| rewind 期间收到 `CancelTurn` | 走 R4 取消路径,标记 turn 取消,不留半截 history |
| `new_args` schema 与子 skill 不符 | 子 skill k 派发时照常走 DispatchPolicy + 子 turn 校验,失败按既有 TurnFailed |

## 7. R1–R5 影响声明(CLAUDE.md 强制)

- **R1 业务零侵入**:`DispatchCheckpoint` / `RewindDispatch` 全是内核通用结构,无 tenant / 无领域名词;业务侧通过 submit Op 使用。✅
- **R2 cache 友好**:rewind 必然让 head 之后失效——这是 **pre-turn 级**改 head 的合法场景(非 mid-turn)。返回 `CompressionResult{cache_invalidated=True, anchor_preserved_until=checkpoint.cache_anchor_before}`,计入 expected,不计 unexpected_breaks。✅
- **R3 可观测**:新增 `dispatch_checkpoint_recorded`(每次派发)、`turn_rewound`(每次 rewind)、`rewind_rejected`(校验失败)。✅
- **R4 可取消**:重推全程透传根 `CancellationToken`,子 skill k 走 `cancel.child()`。✅
- **R5 可 resume**:checkpoint 只记下标,rewind 走切片 + fork,**不物理删** store;JSONL 仍 append-only,旧 thread 可回放。✅

## 8. 未来可选(本期不做,YAGNI)

- **replay 模式**(原方案 B):`RewindDispatch(mode="replay")` 把首跑录下的 call 图 k..N 确定性重放,不重新进 LLM 推理——可复现/可审计场景(如医疗)。作为 `mode` 旋钮叠加在同一 checkpoint 基础设施上,不改 v1 默认 re-reason 行为。
- **放开 entry 约束**:若未来确需「同一 skill 既自治 child 又独立 entry」,另起 ADR 评估 `dispatch.py:175` 的松绑代价。本能力让该需求**不再必要**,故暂不动。

## 9. 测试(边界必测)

`tests/test_dispatch_rewind.py`(全 MockClient):

1. `test_checkpoint_recorded_per_dispatch` —— 自治链跑 N 次 call_skill,得 N 个 checkpoint,下标/截断点正确。
2. `test_rewind_truncates_history_and_anchor` —— rewind(k) 后 history 截到 `history_len_before`,cache_anchor 回退。
3. `test_rewind_re_reasons_downstream` —— rewind(k) 用 new_args 重跑后,MockClient 脚本让根 LLM 走出**不同**下游调用,断言下游自适应。
4. `test_rewind_out_of_range_rejected` —— 越界 dispatch_index → rewind_rejected,history 不动。
5. `test_rewind_on_suspended_turn_rejected` —— 挂起态 rewind 拒绝。
6. `test_rewind_cancel` —— rewind 中途 CancelTurn,无半截 history(R4)。
7. `test_rewind_cache_result` —— 校验返回的 `CompressionResult` 字段(R2)。
8. `test_rewind_append_only_preserved` —— store 仍 append-only,旧 items 未被物理删(R5)。

## 10. 文档义务(收尾红线)

实现完成后,以下**必须**同步,否则 PR 不合并:

- `docs/architecture/capabilities/skill-dispatch.md` 加新 Requirement + Scenario。
- `docs/architecture/agent-loop.md` 活文档更新(turn 内 checkpoint、RewindDispatch Op)。
- 新增 ADR(为什么派发粒度 / 为什么默认 re-reason / 为什么不放开 entry)。
- `examples/step_pipeline/README.md` 修正「纯加法」段,改述为「自治链可直接重试某步(本能力);step_pipeline 是另一种确定性范式」。
- `docs/configurable-knobs.md` 若 RewindDispatch 暴露给业务,补一行。
```
