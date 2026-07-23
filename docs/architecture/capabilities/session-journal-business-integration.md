# SessionJournal 普通业务主链接入能力契约

> 状态：Experimental。关联 ADR 0025。依赖 `session-journal-core` Phase 1。

## 1. 范围

本能力只覆盖显式启用审计的新 Session：

```text
UserMessage → LLM → 基础 Tool / 同步 call_skill → assistant
```

SessionJournal 是执行事实和对话项的唯一可靠事实源。hot history、MessageStore 和 EventMsg 都是
Journal durable ack 之后的内存态或可重建投影，不得领先 Journal，也不得被声明为第二事实源。

未启用 audit 的 EnginePool/AgentEngine/MessageStore 行为保持不变。本阶段不支持已有 Journal、resume、
HITL/审批、suspend、compaction/rewind、memory、instruction 更新、hooks、orchestration、detached spawn、
barrier、peer、跨进程 recovery、redaction、加密、WORM 或外置 blob。

## 2. 唯一事实源与提交顺序

进入对话历史的事实必须表示为 `conversation_item`，并与领域 outcome 在同一个 batch 提交：

```text
submission_accepted + conversation_item(user_message) + submission_applied
llm_response_committed + conversation_item(reasoning/assistant/function_call...)
tool_outcome_committed + conversation_item(function_call_output)
skill_dispatch_finished + thread_terminal + conversation_item(skill_outcome)
```

只有覆盖这些 record id 的 `JournalAck` 返回后，调用方才能：

1. 更新 hot history；
2. 更新 MessageStore 物化投影；
3. 发布受 checkpoint 约束的 LLM delta；
4. 开始下一个 effect 或 terminal transition。

Tool outcome 不得重复写 LLM response 已记录的 `function_call`；output 通过稳定 item/call identity 引用
唯一 call。

## 3. 版本与 identity

业务 payload 使用 frozen、`extra="forbid"` 的 Pydantic V1 DTO，均含 `payload_version=1`，并在进入 core
前转成 canonical JsonValue。现有 Phase 1 初始化三记录是 V0 canonical vectors，保持原 bytes 和 record id。

| 对象 | Identity |
| --- | --- |
| submission | `submission_id` |
| root/child turn | `{thread_id}:{submission_id}:turn:{turn_index}` |
| LLM logical call | `{turn_id}:llm:{iteration}` |
| LLM attempt | `{llm_operation_id}:attempt:{retry_ordinal}` |
| Tool call | `{turn_id}:tool:{call_id}` |
| Skill dispatch | `{tool_operation_id}:skill:{target_skill_id}` |

除初始化 V0 外，record id 固定为：

```text
{operation_id}:{record_type}:{attempt_id-or-none}:{ordinal}
```

相同 id 与相同完整 record 是幂等重试；相同 id 与不同内容必须冲突。

## 4. SessionAuditCoordinator

每个 active Session 恰有一个 coordinator 和一个 SessionJournal lease。root 与全部 child thread 共用该
coordinator，通过 thread/turn/call lineage 区分。

coordinator 必须：

- 串行 append，并只从 durable ack 推进 expected seq；
- 在 effect 前检查 health/lifecycle gate；
- 第一次 Journal IO、integrity 或 ack-uncertain 失败时保存稳定首因、关闭 effect gate、设为
  `RECOVERY_REQUIRED` 并取消 Session root/children；
- 让其他 Session 的 coordinator 保持独立；
- 把投影失败标成 stale，但不冻结 Journal execution；
- 通过唯一、幂等的 finish future 提交 terminal records 并释放单 Session lease。

effect 遵循：

```text
durable intent → at-most-one live dispatch → durable outcome or UNKNOWN
```

不承诺跨进程 exactly-once。

## 5. core 单 Session 关闭

```python
async def close_session(self, lease: SessionLease) -> None: ...
```

`close_session` 必须在 registry/per-session lock 下验证 session id、writer id、writer epoch、lease id，等待该
Session 在途 append，标记 writer closed，并只移除该 writer。错误 lease 必须拒绝；其他 Session writer
继续可写。

`close_session` 不写 `session_ended`。正常终结由 EnginePool 先提交领域 terminal batch，再调用一次
`close_session`。全局 core 归调用方所有，EnginePool 不调用 `core.close()`。

## 6. Bootstrap 与投影

audited bootstrap 固定顺序：

1. 静态 capability validation；
2. 预分配 root thread id；
3. `create_session` durable 提交 V0 初始化三记录；
4. 创建 coordinator；
5. projector 用同一 id 建立带 `audit_required`、Journal Session id、schema version 的空 transcript；
6. 成功后才构造并启动 Engine。

projector 只接受 durable ack 覆盖的 `conversation_item` envelope，按 Journal seq 写默认 JSONL 物化层并
维护 projected seq。投影失败返回 stale；投影可以删除并从 Journal 重放。带 audited marker 的 transcript
不得走 legacy resume。

## 7. Submission 与 lifecycle

audited `AgentEngine.submit()` 与 lifecycle 共用同一个 admission lock。UserMessage 必须先 canonicalize 并
durable 提交 acceptance batch，再把携带 ack 的 token 入 actor queue；禁止 enqueue-first。队列内因此不
存在未 accepted submission。

lifecycle 是 `OPEN → FINISHING → CLOSED`：

- 进入 FINISHING 的胜者关闭 intake、快照全部 durable-accepted queued/in-flight submissions，并创建唯一
  finish future；
- accepted-but-queued work 必须在 `session_ended` 前收敛；
- `AcceptedWork.complete()` 通过 coordinator async callback，在 shield 与 lifecycle lock 内校验
  reservation/work identity、立即退休 map entry 并 set completion Event，只跟踪 pending 或
  accepted-incomplete work；已快照 finish waiter 不丢唤醒，double complete 幂等，CLOSED 后晚完成只清
  unresolved introspection、不复活 Session；
- FINISHING/CLOSED 后的新请求不得 durable accept 或 enqueue，返回 `SessionFinishingError`；
- 并发 release/close 只等待同一 canonical future value，但每个 caller 得到对象与嵌套 failure 独立的
  防御性副本；并发不同 Shutdown id 在 acceptance 前拒绝。

accepted work 收敛后，finish 持有 append lock，重读最新 committed thread-terminal 集合，设置不可逆
terminal seal 并直接提交去重后的 `thread_terminal* + session_ended`。seal 或 CLOSED 后的普通 append 必须
在 core dispatch 前拒绝，因此 `session_ended` 是最终 durable record。

finish result 与 `SessionAuditSnapshot` 分开报告两个事实：

- `audit_complete`：terminal batch 收到 definite durable ack；
- `lease_released`：normal/emergency close 已确定释放 lease。

两者成功为 `true/true`；terminal ack 后 close 失败为 `true/false`，保留 terminal ids 且 health 为
recovery-required；terminal 失败但 emergency close 成功为 `false/true`；两者失败为 `false/false`。
`close_session()` 只释放资源，不能推翻或制造 `session_ended` 事实。

CancelTurn 只取消 target turn 及其 child effect subtree。freeze 和 Shutdown 才取消 Session root。Journal
不可用时 CancelTurn/Shutdown 可作为安全降级动作执行，但不得伪造 durable record，health/introspection
必须报告 `audit_complete=false`；若 emergency close 确定成功则独立报告 `lease_released=true`。

## 8. LLM checkpoint-before-delta

audit 模式只接受能暴露每个真实网络 attempt 的 ModelClient。每个 attempt：

1. `before_attempt` durable 写 `llm_request_committed`；
2. ack 后才 dispatch；
3. provider event 先缓冲；
4. complete/error/cancel 后在 cancellation-independent bounded shield 中 durable 写
   `llm_response_checkpoint`；
5. checkpoint ack 后才按原序发布 delta 或开始下一 attempt；
6. 最终 logical response 与 conversation items 原子提交。

observer/ack 不确定使 attempt 为 UNKNOWN、冻结 Session、禁止 retry。未来 provider 若增加内部 retry，必须
显式接入 observer 才能进入 audit 模式。

## 9. Tool 终态收敛

audit ToolSpec 必须声明 `effect_kind`、`reconciliation`、`can_suspend=False`。hooks、permission、HITL 和
可 suspend Tool 在 effect 前拒绝。

一批 call 先按 call index 原子提交所有 executable/rejected intents，再 dispatch。每个 committed intent
必须得到一个 terminal outcome：`success | error | rejected | cancelled | unknown`。

- 只有未越过 dispatch gate，或 runtime 明确证明 effect 未发生/已知终止，才是 cancelled；
- dispatch 后仅收到取消异常、超时或外部结果不明时是 unknown；
- parent cancel 时 best-effort 取消 sibling，但必须在独立 shield 中收敛全部 intents；
- outcomes 与 function call outputs 按 call index 原子提交；
- 任一 unknown 冻结 Session，不进入下一次 LLM。

## 10. 同步 call_skill

outer Tool intent durable 后提交含完整 definition/body/hash/arguments/provenance 的 `skill_selected`。quota
rejection 写 finished(rejected)，不创建 child。

quota 通过后预分配 child thread id，原子提交 started/thread-created/thread-bound/child-seed。ack 后 child
使用 hot history 运行；投影失败只标 stale。child success/error/cancel 后原子提交
finished/thread-terminal/skill-outcome，outer Tool 随后提交自己的 outcome/output。

child turn identity 包含 child thread id 和 parent submission id。unexpected suspension 是 capability 声明
违约：在产生 HITL effect 前拒绝，并在可能时写 error 后冻结。

## 11. 稳定错误与附件

`StableErrorV1` 字段：`code`、稳定 `class_name`、`failure_class`、可选安全 message/descriptor hash、
`retryable`。禁止持久化任意 `repr()`、traceback、内存地址或 secret。

附件只接受完整 inline base64 content，并在 acceptance 前校验 media type、size、SHA-256、单项和总大小
上限。临时路径、引用型输入、缺失正文、digest/size 不符或超限写安全 submission rejection，不冻结。

## 12. Capability gate

| 维度 | 允许 | 拒绝 |
| --- | --- | --- |
| Op | UserMessage、CancelTurn、Shutdown | 其他 Op |
| Session | 新建 | resume、已有 Journal |
| Store | 默认 JSONL 可重建投影 | custom store/directory、IndexHook |
| Hook/approval | 无 | hooks、permission、HITL |
| Context | 无 compressor/memory/instruction update | compaction、rewind、memory、instruction |
| Skill | atomic/composite、同步 call_skill | orchestration、suspension |
| Spawn/peer | 无 | detached spawn、barrier、peer |
| LLM | attempt-observable | opaque attempt/retry |
| Tool | audit metadata 完整且 non-suspending | metadata 缺失或可 suspend |

静态配置在 EnginePool 构造期验证，Op 在 submission gateway 验证，动态 effect 在 TurnRunner gate 再验证。
拒绝必须发生在 effect 前。

## 13. 验收门槛

- records/core/projector/coordinator/Engine/LLM/Tool/Skill focused tests 全绿；
- cancel 四窗口、并行部分完成、projection stale、Session 隔离和 lifecycle race 全覆盖；
- legacy mode 回归不变；
- full Ruff changed-files、full mypy、full pytest、Sim selfcheck、OpenSpec strict validation 全绿；
- living architecture 与 `docs/capability-matrix.md` 同步；
- 获得明确外部 provider 授权后运行真实 LLM capability matrix，并在最终代码 head 刷新两份 ledger。

最后一项未完成时，不得标 OpenSpec 完成、archive 或 merge。
