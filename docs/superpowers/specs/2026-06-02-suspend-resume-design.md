# 通用挂起 / Resume 原语设计

> 状态:已通过 brainstorming 评审,待用户复审 → 转 writing-plans。
> 日期:2026-06-02 · 分支:`feat/suspend-resume`
> 影响红线:R1(业务零侵入)/ R2(cache 友好)/ R3(可观测)/ R4(可取消)/ R5(可 resume)——逐条评估见 §7。

## 1. 背景与问题

当前 HITL(human-in-the-loop)只有 permission gate 一种,且实现为**阻塞 await**:
`PermissionPolicy.check()` 在 `ask` 模式下 `await prompter.prompt()`,该 await 埋在
tool 执行最深处(`builtins` → `dispatch_batch` → `_sample_once` → `run_turn`)。一旦命中,
**整条 turn 协程连同 engine actor 全程挂起驻留内存**。若审批需要数小时,实例就被占用数小时,
无法回收 / 缩容 / 跨进程恢复。

已有的 `engine-resume-by-thread-id` 只能在 **turn 边界**从 JSONL 重建会话,
**无法**恢复一个"turn 中途挂着的待审批"。

### 目标

把"turn 中途阻塞等待"改造成**通用挂起原语**:

- 一个 turn 可在任意点产出 `TurnSuspended([pending...])` 作为**正常结局**(而非阻塞),随后释放实例。
- 业务侧之后凭 `thread_id` + `request_id` 提交 `Resume` Op 续跑。
- 覆盖两大类挂起:
  - **人类输入类**:权限审批(permission)、表单填写(form)、外部数据(data)。
  - **系统态类**:LLM 限流 / QPS / 余额不足 / key 鉴权失败 / LLM 报错(`system_retry`)。
- 一次可并存多个挂起点,各带独立 `request_id`,batch resume 用 `{request_id: payload}`。

### 释放分级(用户决策)

- **tier-1(默认,快)**:释放 engine actor / TurnRunner 协程,engine 留在 Pool 的 suspended 槽,
  同进程内 resume 不必从 store 重建。
- **tier-2(缩容 / 进程可退)**:Pool 驱逐该 engine,挂起状态全在 store;进程可退出,
  之后凭 `thread_id` 从 JSONL 重建 + 读 `SuspensionRecord` 续跑。

## 2. 参照实现与借鉴(参照 X,差异 Y)

横向研究了 `<opensource>/` 四个内核的"交互等待":

| 内核 | 等待模型 | 持久化挂起 | 进程可退后 resume |
| --- | --- | --- | --- |
| codex (Rust) | 阻塞 `oneshot` channel + `Op::ExecApproval{id,decision}` | ❌ 仅内存 | ❌ |
| **openclaw** (TS) | **tool 早返回 `approval-pending` + 决定落盘 + 重入** | ✅ `exec-approvals.json` | ✅ |
| hermes (Py) | 阻塞 `threading.Event` + `resolve_gateway_*()` | ❌ 仅内存 | ❌ |
| claw-code (Rust) | 同步阻塞 `prompter.decide()`(stdin) | ❌ 仅内存 | ❌ |

**结论**:codex / hermes / claw-code 都是内存阻塞,**都不支持进程可退 + 跨进程 resume**;
**openclaw 是唯一异类,也正是本设计的范式**(挂起=turn 早返回结局 + 决定存外部 + 重入续跑)。

借鉴矩阵:

| 借鉴点 | 来源 | 差异(taifeng 的改法) |
| --- | --- | --- |
| 挂起=turn 早返回结局、不阻塞协程;决定存外部、之后重入 | openclaw | openclaw 决定存独立 json + 靠"重跑 tool 重查";taifeng 复用 **`function_call` 无 `function_call_output` 的 history-gap** 表示挂起点,resume 填 output,**不重跑 tool** |
| 请求用 typed `EventMsg` 外发 + 关联 id + **独立 Resume Op** + pending-map 按 id 索引 | codex | codex 每种 ask 一个 Op;taifeng **收敛成一个通用 `Resume` Op**,`reason` 用 typed enum 区分 |
| 会话 resume 从 rollout 文件重建 | codex `resume_thread_from_rollout` | codex **不持久化 turn 中途 pending approval**;taifeng **额外落 `SuspensionRecord` 标记中途断点**,使 mid-turn resume 可行 |
| 多挂起点并存,按 id 区分 | codex pending HashMaps | taifeng 一个 `TurnSuspended([pending...])`,各带 `request_id` |
| 选择/表单带 `choices` 或自由文本 | hermes clarify | 归一到通用 `payload_schema`(JSON Schema),permission 只是其特例 |

所有移植后的文件按 CLAUDE.md 要求标注「参照 X,差异 Y」。

## 3. 架构与新增组件

```
src/taifeng/
├── suspend/                 # 【新】通用挂起原语(业务无关)
│   ├── reason.py            #   SuspendReason 枚举 + PendingRequest 数据契约
│   ├── record.py            #   SuspensionRecord(可序列化断点标记,落 store)
│   └── resolver.py          #   SuspensionResolver:把 {request_id: payload} 配回 pending
├── loop/
│   ├── submission.py        # 【改】新增 Resume Op(加进 Op Union)
│   ├── event.py             # 【改】新增 TurnSuspended / SuspensionResolved / SuspensionResolveRejected
│   ├── turn.py              # 【改】run_turn 认 SuspendSignal → 退栈为 TurnSuspended 结局
│   ├── tool_batch.py        # 【改】dispatch_batch 收集整批 SuspendSignal(不 fail-fast)
│   └── engine.py            # 【改】_handle_resume:配 resolution、续跑;suspended 态管理
├── permission/
│   └── types.py             # 【改】新增 SuspendingPrompter:ask→抛 SuspendSignal(而非阻塞)
└── conversation/
    └── store.py / models.py # 【改】SuspensionRecord 的 append/load(复用 JSONL 追加写)
```

核心三动作:

1. **挂起点抛信号**:permission `ask`、表单 tool、LLM 可恢复错误(自动 retry 耗尽后)处,
   不再阻塞 `await`,而是抛 `SuspendSignal(PendingRequest)`。
2. **turn 退栈为结局**:`dispatch_batch` 收集整批所有 `SuspendSignal`(支持多挂起点)→
   turn 把每个待批 tool call 作为 `function_call`(无 output)落盘 + 写 `SuspensionRecord` →
   返回 `TurnSuspended([pending...])`,emit 事件,协程彻底退栈。
3. **Resume Op 续跑**:`Resume(thread_id, {request_id: payload})` 入队 →
   `Pool.acquire(resume_thread_id)`(需要时从 JSONL 重建)→ `SuspensionResolver` 配对 → turn 续跑。

## 4. 数据契约(typed,R1 业务零侵入)

```python
# suspend/reason.py
class SuspendReason(str, Enum):
    """挂起原因分类 —— 决定 resume 时的续跑语义。"""
    PERMISSION = "permission"      # 等权限审批 → decision 回填 gate 结果
    FORM = "form"                  # 等用户填表 → payload 成 tool output
    DATA = "data"                  # 等外部数据 → payload 成 tool output
    SYSTEM_RETRY = "system_retry"  # 限流/余额/key/LLM错 → resume 即重试同次 sample

@dataclass(frozen=True)
class PendingRequest:
    request_id: str                # 关联 id(对标 codex call_id)
    reason: SuspendReason
    payload_schema: dict           # 业务/前端据此渲染表单或审批 UI(JSON Schema)
    related_call_id: str | None    # 关联的 function_call(人类输入类必有;系统态为 None)
    detail: dict                   # 不透明上下文(scope/target/command/failure_class 等),taifeng 不解析

@dataclass(frozen=True)
class SuspensionRecord:            # 落 store 的可序列化断点标记
    record_id: str                 # 幂等键(重复 resume 检测)
    thread_id: str
    submission_id: str
    turn_index: int
    pending: tuple[PendingRequest, ...]
    created_at: int                # 业务侧戳(R1:src 内不取系统时钟,由构造方传入)

# loop/submission.py 新增
class Resume(BaseModel):
    kind: Literal["resume"] = "resume"
    thread_id: str
    resolutions: dict[str, Any]    # {request_id: payload};payload 形状由 reason 决定
```

- `SuspendSignal` 是**内部控制流异常**(类似既有 `DispatchVerdict`),**不进 `LLMError` 体系**
  —— 它不是错误,是正常挂起。
- `TurnSuspended` 是 `run_turn` 的**合法返回结局**,与 `TurnComplete` 并列。
- `PendingRequest.detail` / `Resume.resolutions` 的 payload 都是**不透明 JSON**,taifeng 不解析其 keys(R1)。

## 5. 数据流

### 5.1 人类输入类挂起(permission / form / data)

```
TurnRunner._dispatch_tools
  └─ dispatch_batch 并发跑 N 个 tool call
       ├─ tool_X: SuspendingPrompter.ask → 抛 SuspendSignal(reason=permission, related_call_id=X)
       ├─ tool_Y: 表单 tool → 抛 SuspendSignal(reason=form, related_call_id=Y)
       └─ tool_Z: 正常完成 → function_call_output(Z)
  └─ 收集:[signal_X, signal_Y] + [output_Z]
  └─ 落盘:function_call(X)、function_call(Y) 无 output;function_call(Z)+output(Z)
  └─ 写 SuspensionRecord{pending=[X,Y]}
  └─ raise → run_turn 捕获 → 返回 TurnSuspended([X,Y])
AgentEngine：emit TurnSuspended([X,Y]) → 释放(tier-1 留 pool / tier-2 驱逐)
─────────────── 数小时后 ───────────────
Submission(Resume{thread_id, {X: decision_allow, Y: form_payload}})
AgentEngine._handle_resume
  └─ Pool.acquire(resume_thread_id)（需要则从 JSONL 重建 history + SuspensionRecord）
  └─ SuspensionResolver 配对:
       ├─ X(permission,allow) → 此刻执行 tool X → 填 function_call_output(X)
       │  (permission deny → 填 error function_call_output,与现有 deny 语义一致)
       └─ Y(form) → form_payload 直接成 function_call_output(Y)
  └─ 清除 SuspensionRecord → turn 从 sample loop 续跑(history 已补齐 X、Y output)
```

### 5.2 系统态挂起(限流 / 余额 / key —— 兜底)

```
_sample_once → ModelClientSession.stream
  └─ RateLimitError(retryable) → retry_async 自动退避重试 ≤3 次(既有机制,emit provider_retry)
  └─ 3 次仍失败 / AuthenticationError(retryable=False,立即)
       → 转 SuspendSignal(reason=SYSTEM_RETRY, related_call_id=None,
                          detail={failure_class, retry_after_seconds})
  └─ SuspensionRecord.pending 仅含这一个
  └─ 返回 TurnSuspended → 释放
─────── 业务侧充值 / 换 key / 等限流过 后 ───────
Resume{thread_id, {req_id: {"action": "retry"}}}   # 系统态 payload 只需 retry/abort
  └─ Resolver 识别 SYSTEM_RETRY → 不动 history → 重跑那次 _sample_once(获全新 retry 预算)
```

### 5.3 LLMError 转挂起 vs 硬失败 的判据

- **转 `SYSTEM_RETRY` 挂起**:`error.retryable` 为真,或 `failure_class ∈ {provider_auth, provider_quota/balance}`
  —— 等外部条件清除后**重跑同次 sample 必然能过**。
- **照旧硬失败(抛 LLMError)**:`ContentFilterError` / `ContextOverflowError` / `InvalidRequestError`
  —— **确定性失败**,重跑同次 sample 也没用,不挂起。
- (可能新增 `provider_quota` / `provider_balance` failure_class;`detail.failure_class` 让业务知道
  该"等限流"还是"充值/换 key"。)

## 6. 错误处理与边界

- **resolution 不全 / 多余**:缺某 pending 的 `request_id`,或带不存在的 id → 拒绝,
  emit `SuspensionResolveRejected`(禁 silent fallback)。
- **部分 resume**:**不允许**。必须一次补齐该 record 全部 pending 才续跑(避免半挂起态;
  分批收集由业务侧攒齐再提交)。
- **resume 时 thread 不存在 / 已完成 / SuspensionRecord 缺失**:拒绝 + 明确错误码
  (对标既有 `resume_thread_id not found` 不静默回退)。
- **重复 Resume(同 record 两次)**:`SuspensionRecord` 消费即清除 + 幂等键 `record_id`;
  第二次命中"已消费" → 拒绝。
- **挂起期间来 CancelTurn**:清除 `SuspensionRecord`,turn 终结为 cancelled(R4)。
- **payload 不符 `payload_schema`**:Resolver 校验失败 → 拒绝(系统边界校验)。

## 7. R1–R5 影响

| 红线 | 影响与落实 |
| --- | --- |
| **R1 业务零侵入** | `suspend/` 全 typed、无业务词;`detail` / `payload` 不透明 JSON,taifeng 不解析。✅ |
| **R2 Cache 友好** | 补齐 `function_call_output` 是 **tail append**(不动 head),符合 mid-turn 只改 tail;`TurnSuspended` 携带 `cache_invalidated`(tier-2 跨进程必失效;tier-1 同进程尽量保 anchor)。✅ |
| **R3 可观测** | 新增 `turn_suspended` / `suspension_resolved` / `suspension_resolve_rejected` EventMsg。✅ |
| **R4 可取消** | 挂起态可被 CancelTurn 清理;协程已退栈,不阻塞主 actor。✅ |
| **R5 可 resume** | `SuspensionRecord` 走既有 JSONL 追加写;复用 `resume-by-thread-id`。✅ |

## 8. 测试计划

`tests/test_suspend.py` + 扩 `tests/test_engine_e2e.py`(全部走 MockClient,CI 禁真实 API):

- 多挂起点并存(batch 内 permission + form 同时挂)
- 部分 resolution → 拒绝
- permission allow / deny resume
- form / data payload 回填为 function_call_output
- SYSTEM_RETRY:自动 retry 3 次耗尽 → 挂起 → resume 重跑 sample
- 重复 resume 幂等(record 已消费 → 拒绝)
- 挂起中 CancelTurn → cancelled
- payload schema 校验失败 → 拒绝
- tier-2 进程重建 resume(MockClient 模拟跨实例,从 JSONL + SuspensionRecord 重建续跑)
- 边界:空 resolutions、超长 payload、未知 request_id、thread 不存在

## 9. 文档义务(实现完成后)

- 新增能力契约 `docs/architecture/capabilities/suspend-resume.md`(数据契约 + 行为契约)+ 更新其 README 索引。
- 更新 `docs/architecture/agent-loop.md`(loop/tool 变更)、`docs/architecture/<permission 篇>`。
- ADR:`docs/decisions/NNNN-suspend-resume-primitive.md` 记"为什么挂起=turn 结局而非阻塞 channel"
  + "为什么额外持久化 SuspensionRecord"(对 codex 的差异)。
- 更新 `docs/configurable-knobs.md`(新增 Resume Op、SuspendingPrompter 配置、retry-then-suspend 阈值)。
- **硬约束**:architecture / 契约未同步 → PR 不合并。
