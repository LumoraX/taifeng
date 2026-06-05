# 设计:Turn 内任意节点可寻址 Rewind(自治链任意节点直接重试)

- 日期:2026-06-05
- 状态:**已实现**(2026-06-05)。活文档真相见 [`capabilities/turn-rewind.md`](../../architecture/capabilities/turn-rewind.md) + ADR 0014
- 实现差异(本设计 → 落地):① `turn_root` 节点**收敛进首个 iteration 节点 `it1`**(截点等价,不单列);② `retry_tool` v1 假定**串行派发**(`max_parallel_tool_calls=1`,默认),并行批次内部分重试不支持;③ `new_args` 经内存改写悬空 fc 实现
- 适用红线:R1(业务零侵入)/ R2(cache 友好)/ R3(可观测)/ R4(可取消)/ R5(可 resume)—— 本能力改 turn/dispatch 模型,按 CLAUDE.md 必须逐条声明影响(见 §7)

## 1. 问题陈述

自治链「一键跑完」= 用户**一条** `UserMessage` → 根 entry skill 的 LLM 在**同一个 root turn 内**多圈采样、连续 `call_skill(B)`、`call_skill(C)`… → **一个** `turn_completed`。整条链的「步」是这一个 turn 里的**采样圈 + 嵌套 tool 调用**,中间**没有 user-message 边界**。

由此带来的缺口:

1. **无法重跑中间任意节点**。现有 [`ThreadRollback`](../../../src/taifeng/loop/submission.py) 的回滚粒度是「一轮 = user_message 到下一个 user_message」,比 turn 内的**采样圈 / 工具派发**粗一到两级。自治链整条只有一轮,ThreadRollback 要么不退、要么整条全废。既不能「retry 某一次 LLM loop」,也不能「retry 某一次 call_skill」。

2. **README 的「entry:true 纯加法」是伪命题**。`examples/step_pipeline/README.md` 声称给子 skill 加 `entry: true` 后「既能被自治链 `call_skill` 派发,又能被业务编排单独拉起」是纯加法。但 [`dispatch.py:175`](../../../src/taifeng/skill/dispatch.py) 在 `target.entry` 为真时直接 `reject("cannot_call_entry_skill")`,能力契约 [`skill-dispatch.md`](../../architecture/capabilities/skill-dispatch.md) 明文「跨 entry skill 派发必须开新 session」。即 `entry: true` 这个动作**本身就掐断 call_skill 路径**——采用 step_pipeline 等于**替换**自治编排,不是叠加。

## 2. 目标与非目标

**目标**

- 在 taifeng 内核侧,把一次 turn 的执行轨迹拆成**一张完整、可寻址的回访节点表**,业务侧可对**任意节点直接 retry**:既能 retry 某一次 LLM loop 采样,也能 retry 某一次工具 / 子 skill 派发。
- 重试发生在**同一次 turn 内**,自治性按需保留(re-reason)或精准(retry_tool)。
- 通用内核原语,落 `src/taifeng/`,**零业务概念**(守 R1);不只服务医疗,任何 turn 白捡。
- 子 skill 全程保持 `entry: false`——**绕开 entry/call_skill 矛盾**,不靠把子 skill 变 entry。

**非目标(本期不做)**

- 不做「录后确定性重放整条 call 图」模式;只在 §8 留作未来可选 policy。
- 不改 `entry: true` 语义,不放开「entry skill 可作为 call_skill target」(另一条 ADR)。
- 不改 step_pipeline example 的业务层编排器(保留为另一种确定性范式)。
- 不做跨 turn / 跨 session 的 rewind(只作用在**活着的 root turn**内)。

## 3. 核心设计:统一回访节点模型

三类节点,落在**同一个 checkpoint 结构** + **同一个 Op `Rewind`** 上,引擎按 `kind` / `mode` 分流。

### 3.1 节点分类(完整回访节点表)

| 节点 kind | 锚点(history 下标) | rewind 它 = | mode 支持 |
| --- | --- | --- | --- |
| `turn_root` | user message 处 | 整条 turn 重来 | `re_reason` |
| `iteration` | 每圈 [`_sample_once`](../../../src/taifeng/loop/turn.py) 前 | **重采样第 k 圈**,LLM 重产 text/tool calls、下游全重决 | `re_reason` |
| `dispatch` | 每次工具 / `call_skill` 的 `function_call` 前后 | 见下两切点 | `retry_tool` + `re_reason` |

**dispatch 节点的两个切点**(对应「两种都要」反馈):

- `retry_tool`:切点 = `function_call` 之后、`function_call_output` 之前。**保留 assistant「决定调它」的动作**,只用 `new_args`(或原 args)重跑该工具、替换其 output。若 `new_args` 改了入参,**同步改写该 `function_call` 的 arguments** 以保持历史自洽,并落一条 marker 注明。
- `re_reason`:切点 = 该派发所属 `iteration` 圈的采样前(等价于定位到包含它的 iteration 节点)。截到 assistant 消息前 → 重采样 → LLM 重新决定要不要调、调什么。

> 注:`dispatch.re_reason` 与对应 `iteration` 节点是**同一截点的两个入口**(一个从「这一步」找上去,一个直接选那一圈),引擎归一到同一截断逻辑,不重复实现。

### 3.2 Checkpoint 结构与记录(`loop/turn.py`)

```python
@dataclass(frozen=True)
class RewindCheckpoint:
    """turn 执行轨迹上的一个可回退锚点(append-only 不破:只记下标)。"""
    node_id: str               # turn 内稳定 id(如 "it3" / "disp2")
    kind: Literal["turn_root", "iteration", "dispatch"]
    history_len: int           # 截断点 = 该 history 长度(retry_tool 另记 output 前的内层下标)
    cache_anchor: int          # 回退时还原的 cache_anchor_index
    iteration_index: int       # 所属采样圈(dispatch 借此映射 re_reason 截点)
    # 仅 dispatch:
    call_id: str | None = None
    target_id: str | None = None    # 子 skill / 工具名
    inner_history_len: int | None = None  # retry_tool 切点(function_call 后、output 前)
    args_digest: str | None = None        # 供 UI / 审计
```

记录点:
- **iteration 节点**:[turn.py:388](../../../src/taifeng/loop/turn.py) `_sample_once` 调用前,记 `len(history_buffer)` + cache_anchor。
- **dispatch 节点**:[turn.py:785-798](../../../src/taifeng/loop/turn.py) 追加 `function_call` / `function_call_output` 处,记两个下标——`history_len` = **所属 iteration 采样前**(re_reason 切点,与该圈 iteration 节点同值,因 assistant 消息原子、不可切在并行 tool_call 中间),`inner_history_len` = `function_call` 后 / `function_call_output` 前(retry_tool 切点)。**覆盖所有工具派发,不止 call_skill**。
- 全部存 root `TurnRunner.rewind_checkpoints: list[RewindCheckpoint]`,turn 结束随 `history_buffer` / `cache_anchor_index` 一并回写 engine(engine.py:745-747 同路径)。
- checkpoint 是侧录,不进 LLM 上下文、不影响 prompt 指纹。

### 3.3 统一 Op `Rewind`(`loop/submission.py`)

```python
class Rewind(BaseModel):
    """回退到 turn 内任一节点并重推。"""
    kind: Literal["rewind"] = "rewind"
    node_id: str                                   # 指向某 checkpoint
    mode: Literal["retry_tool", "re_reason"] = "re_reason"
    new_args: dict[str, Any] | None = None         # 仅 dispatch + retry_tool 有意义
```

业务侧另有只读入口 `engine.rewind_nodes() -> list[RewindCheckpoint]` 取「完整回访节点表」供 UI 渲染可点节点。

### 3.4 主动重推(`loop/engine.py`)

新增 `_handle_rewind(submission_id, op)`,= `_handle_rollback` 的截断三件套 + `_handle_resume` 的主动重推,按 `kind`/`mode` 选截点:

```
async def _handle_rewind(sub_id, op):
    1. cp = lookup(op.node_id);校验(存在 / 有活跃可 rewind 的 root turn / mode 与 kind 相容)
       - 不满足 → 拒绝并 emit rewind_rejected(reason=...)(§6),绝不静默 no-op
    2. cut = cp.inner_history_len if (kind==dispatch and mode==retry_tool) else cp.history_len
    3. async with self._lock:
       - self._history = self._history[:cut]
       - self._cache_anchor_index = min(anchor, cp.cache_anchor)
       - 丢弃 history_len >= cut 的 checkpoint(已失效)
    4. 落 system_injection marker:"[rewind] node=<id> kind=<k> mode=<m>"(同 rollback marker 范式)
    5. 重建 root TurnRunner(截断后 history):
       - retry_tool:用 new_args(或原 args)重跑该工具/子 skill k → append 新 output → 续推根 turn
       - re_reason:直接从截点重采样(不预跑工具,让 LLM 重决)
    6. 全程透传同一 CancellationToken(R4);子 skill 派发走 cancel.child()。
```

## 4. 数据流(retry 第 k 次 call_skill,mode=retry_tool)

```
业务 submit(Rewind(node_id="disp_k", mode="retry_tool", new_args?))
  → _handle_rewind
     ├─ cp=lookup;校验(活跃 turn / mode 与 dispatch 相容)
     ├─ history 截到 cp.inner_history_len(保留 assistant 的 function_call)
     ├─ cache_anchor 回退 → emit cache 失效(expected,R2)
     ├─ emit EventMsg.turn_rewound { node_id, kind=dispatch, mode=retry_tool }(R3)
     ├─ (new_args 改了入参 → 改写 function_call.arguments + marker)
     ├─ 重跑子 skill k(cancel.child(),R4) → append 新 function_call_output
     └─ 根 LLM 续采样到终结 → MessageStore.append → TurnComplete
```

(mode=re_reason 时:截到 assistant 消息前,不预跑工具,直接重采样。)

## 5. 模块改动清单

| 模块 | 改动 | 行数预算 |
| --- | --- | --- |
| `loop/turn.py` | `RewindCheckpoint`;iteration 前 + 每次工具派发处记 checkpoint;runner 暴露 `rewind_checkpoints` | ~70 |
| `loop/submission.py` | `Rewind` Op + 并入 `Op` union | ~25 |
| `loop/engine.py` | `_handle_rewind`(retry_tool / re_reason 分流);dispatch 分支;`rewind_nodes()` 只读入口;状态回写带 checkpoints | ~130 |
| `loop/event.py` | `RewindCheckpointRecorded` / `TurnRewound` / `RewindRejected` EventMsg(R3) | ~40 |
| `context/` | rewind 返回 `CompressionResult{cache_invalidated, anchor_preserved_until}`(R2) | ~15 |
| `docs/architecture/capabilities/skill-dispatch.md`(或新 `turn-rewind.md` 契约) | 新 Requirement「回访节点表 + Rewind」 | 契约 |
| `docs/architecture/agent-loop.md` | 活文档同步(节点模型 / Rewind Op) | 活文档 |
| `docs/decisions/NNNN-turn-rewind.md` | ADR:为什么三类节点 / 为什么 dispatch 两切点 / 默认 re_reason / 不放开 entry | ADR |
| `examples/step_pipeline/README.md` | 修正「纯加法」伪命题,指向本能力 | 文档 |
| `tests/test_turn_rewind.py` | 新测试(见 §9) | 新文件 |

## 6. 错误与边界(禁止 silent fallback)

| 场景 | 行为 |
| --- | --- |
| `node_id` 不存在 / checkpoint 已失效 | 拒绝,emit `RewindRejected(reason="unknown_node")`;history 不动 |
| 无活跃可 rewind 的 root turn(turn 已彻底结束) | 拒绝,reason=`no_rewindable_turn` |
| `mode` 与 `kind` 不相容(如对 iteration 用 `retry_tool`) | 拒绝,reason=`mode_kind_mismatch` |
| turn 处于挂起(HITL)态 | v1 拒绝,reason=`turn_suspended`(挂起态 rewind 留待后续) |
| rewind 期间收到 `CancelTurn` | 走 R4 取消路径,不留半截 history |
| `retry_tool` 的 `new_args` 与工具 schema 不符 | 工具/子 skill 派发时照走既有校验,失败按 TurnFailed |

## 7. R1–R5 影响声明(CLAUDE.md 强制)

- **R1 业务零侵入**:`RewindCheckpoint` / `Rewind` 全是内核通用结构,无 tenant / 无领域名词;业务侧通过 submit Op + `rewind_nodes()` 使用。✅
- **R2 cache 友好**:rewind 让截点之后失效——属 **pre-turn 级**改 head 的合法场景(非 mid-turn 动 head)。返回 `CompressionResult{cache_invalidated=True, anchor_preserved_until=cp.cache_anchor}`,计 expected,不计 unexpected_breaks。✅
- **R3 可观测**:新增 `rewind_checkpoint_recorded`(记节点)、`turn_rewound`(每次 rewind)、`rewind_rejected`(校验失败)。✅
- **R4 可取消**:重推全程透传根 `CancellationToken`,子 skill 走 `cancel.child()`。✅
- **R5 可 resume**:checkpoint 只记下标,rewind 走切片 + fork,**不物理删** store;JSONL 仍 append-only,旧 thread 可回放。✅

## 8. 未来可选(本期不做,YAGNI)

- **replay 模式**:`Rewind(mode="replay")` 把首跑录下的 call 图确定性重放、不重进 LLM——可复现/可审计场景。叠加在同一 checkpoint 基建上,不改 v1 默认。
- **放开 entry 约束**:本能力让「同一 skill 既自治 child 又独立 entry」不再必要;若未来确需,另起 ADR 评估 `dispatch.py:175` 松绑代价。
- **挂起态 rewind**:HITL 挂起的 turn 内 rewind,v1 拒绝,后续按需补。
- **压缩等内核动作作为可寻址节点**(TODO):pre/mid-turn compress、instruction 热更等「非采样、非工具」的内核动作,v1 不进回访节点表;后续若需「回退到某次压缩前」再扩 `kind`。本期节点只覆盖 turn_root / iteration / dispatch 三类。

## 9. 测试(边界必测)

`tests/test_turn_rewind.py`(全 MockClient):

1. `test_checkpoints_cover_iterations_and_dispatches` —— 多圈采样 + 多次工具派发,节点表含全部 iteration + dispatch 节点,下标正确。
2. `test_rewind_iteration_re_samples` —— rewind 某 iteration 节点 → 截到该圈采样前,MockClient 脚本让重采样走出**不同**下游,断言自适应。
3. `test_rewind_dispatch_retry_tool` —— retry_tool:保留 assistant function_call,用 new_args 重跑子 skill、替换 output;function_call.arguments 被改写。
4. `test_rewind_dispatch_re_reason` —— re_reason:截到 assistant 前重采样,等价于其 iteration 节点截点。
5. `test_rewind_history_and_anchor` —— 截点 / cache_anchor 回退正确(两种 mode 各验)。
6. `test_rewind_unknown_node_rejected` / `test_rewind_mode_kind_mismatch_rejected` —— 拒绝路径,history 不动。
7. `test_rewind_on_suspended_turn_rejected` —— 挂起态拒绝。
8. `test_rewind_cancel` —— rewind 中途 CancelTurn,无半截 history(R4)。
9. `test_rewind_cache_result` —— `CompressionResult` 字段正确(R2)。
10. `test_rewind_append_only_preserved` —— store 仍 append-only,旧 items 未物理删(R5)。

## 10. 文档义务(收尾红线)

实现完成后,以下**必须**同步,否则 PR 不合并:

- `docs/architecture/capabilities/`:新增 `turn-rewind.md` 契约(回访节点表 + Rewind Op 生命周期),并在 `skill-dispatch.md` 交叉引用。
- `docs/architecture/agent-loop.md` 活文档更新(节点模型、Rewind Op、checkpoint 记录点)。
- 新增 ADR(三类节点 / dispatch 两切点 / 默认 re_reason / 不放开 entry)。
- `examples/step_pipeline/README.md` 修正「纯加法」段,改述为「自治链可直接重试任意节点(本能力);step_pipeline 是另一种确定性范式」。
- `docs/configurable-knobs.md` 补 `Rewind` Op + `rewind_nodes()` 一行。
