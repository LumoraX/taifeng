# ADR 0012: 通用挂起 / resume 原语（turn 早返回结局 + 持久化断点 + 单 Resume Op）

- 状态：Accepted
- 日期：2026-06-02
- Related: spec `docs/superpowers/specs/2026-06-02-suspend-resume-design.md`；能力契约 `docs/architecture/capabilities/suspend-resume.md`
- Supersedes:

## 背景

HITL（human-in-the-loop）原本只有 permission gate 一种，且实现为**阻塞 await**：`PermissionPolicy.check()` 在 `ask` 模式下 `await prompter.prompt()`，该 await 埋在 tool 执行最深处（`builtins` → `dispatch_batch` → `_sample_once` → `run_turn`）。一旦命中，**整条 turn 协程连同 engine actor 全程挂起驻留内存**——若审批需数小时，实例就被占用数小时，无法回收 / 缩容 / 跨进程恢复。已有的 `engine-resume-by-thread-id` 只能在 **turn 边界**从 JSONL 重建会话，**无法**恢复一个"turn 中途挂着的待审批"。

同时另有一个更窄的提案 `hitl-user-input-suspend-resume`：仅为"LLM 向人类发问、等回答"设计独立的 `request_user_input` 工具 + 独立 `provide_tool_result` Op。

横向研究 `<opensource>/` 四个内核的"交互等待"：

| 内核 | 等待模型 | 持久化挂起 | 进程可退后 resume |
| --- | --- | --- | --- |
| codex (Rust) | 阻塞 `oneshot` channel + `Op::ExecApproval{id,decision}` | ❌ 仅内存 | ❌ |
| **openclaw** (TS) | **tool 早返回 `approval-pending` + 决定落盘 + 重入** | ✅ `exec-approvals.json` | ✅ |
| hermes (Py) | 阻塞 `threading.Event` + `resolve_gateway_*()` | ❌ 仅内存 | ❌ |
| claw-code (Rust) | 同步阻塞 `prompter.decide()`（stdin） | ❌ 仅内存 | ❌ |

## 决策

### D1：挂起 = turn 的一个**结局**（early-return + 释放实例），而非阻塞 channel / await

挂起点不再 `await` 一个会阻塞协程的 channel / event，而是抛 `SuspendSignal`（内部控制流异常，**不继承 LLMError**——挂起不是错误）；`run_turn` 把它退栈为 `end_reason="suspended"` 的**正常结局**，与 `completed` / `cancelled` / `error` 并列。turn 协程彻底退栈，engine 实例可被 Pool 驱逐、进程可退出。

**理由**：codex 的 `oneshot` channel、hermes 的 `threading.Event`、claw-code 的 stdin `prompter.decide()` 三者都是**内存阻塞**——挂起期间协程 / 线程 / 进程必须活着，**无法释放实例**，也就无法缩容、无法跨进程恢复。**只有 openclaw 的"tool 早返回 + 重入"范式能释放进程**，本设计即采纳该范式。把挂起做成结局而非阻塞，是"实例可回收 + R5 跨进程 resume"的前提。

### D2：额外持久化一条 `SuspensionRecord`（`suspension` ResponseItem），标记 turn 中途断点

挂起时除了 history 里天然存在的 `function_call`-无-`function_call_output` gap，**额外**落一条 `kind="suspension"` 的 ResponseItem（含 `record_id` / `pending[...]` / `turn_index` / `created_at`）。

**理由**（对 codex `resume_thread_from_rollout` 的差异）：codex 从 rollout 文件重建会话，但**不持久化 turn 中途的 pending approval**——它的 approval 只在内存 channel，进程退了就丢。taifeng 要支持**进程可退后**恢复 mid-turn 挂起，必须把断点本身落盘。两个具体动因：

1. **`system_retry` 没有 tool_call gap**：限流 / 鉴权失败发生在 `_sample_once`（采样阶段），history 里**没有**对应的 `function_call`，光靠 gap 无法表达"这里挂着一个待重试"。必须有显式 record。
2. **不同 reason 需要 typed 续跑语义**：permission / form / data / system_retry 的 resume 动作各异（执行 tool / 回填 output / 重跑 sample）。record 携带 `reason` + `payload_schema` + `related_call_id`，使 resolver 能按类型分流；纯 gap 不携带这些语义。

"已消费"判定不靠改写已落盘 item（JSONL 追加写不可变），而靠**追加一条 resolved-marker**（`system_injection` source=`suspend_resolved`），`_find_active_suspension` 据此过滤——resume 与 R4 取消共用同一机制，重复 Resume 自然被拒（幂等）。

### D3：一个通用 4-reason `Resume` Op，吸收更窄的 `hitl-user-input-suspend-resume` 提案

不为每类挂起各开一个 Op（codex 风格：`ExecApproval` / ...），也**不**保留两条平行通道（通用挂起 + 独立 `request_user_input` 链路）。统一为：**单个 `Resume(thread_id, resolutions)` Op** + `SuspendReason` typed 枚举（`permission` / `form` / `data` / `system_retry`）区分续跑语义。原 `request_user_input` 工具保留为内置工具，但其触发归一到 `SuspendSignal(reason=DATA)` → `TurnSuspended` → `Resume`，payload 经 resolver 回填成该 call 的 `function_call_output`——**不**引入独立 `provide_tool_result` Op。

**理由**：两条平行通道会带来双份 Op、双份事件、双份 resume 路径与双份幂等 / 取消逻辑，维护面翻倍且边界（多挂起点并存时谁先谁后）难界定。收敛成"一个 Op + typed reason"后：多挂起点（permission + form 同批）能落进**同一条** `SuspensionRecord`、用一次 batch `resolutions` 补齐；幂等 / 取消 / 跨进程重建只需实现一遍。窄提案是通用原语的**真子集**，故被吸收而非并存。

## 备选方案（被拒）

- **阻塞 channel / event（codex / hermes 范式）**：❌ 无法释放实例、无法跨进程 resume（见 D1）。
- **只靠 history-gap、不落 SuspensionRecord**：❌ `system_retry` 无 gap 可挂、reason 续跑语义无处携带（见 D2）。
- **每类挂起一个 Op + 独立 `request_user_input` 链路并存**：❌ 双份维护面、多挂起点并存边界难界定（见 D3）。
- **改写已落盘 suspension item 的 `resolved` 标志位**：❌ 违反 JSONL 追加写不可变；改用追加 resolved-marker（见 D2）。

## 影响

### 公共 API 变更（兼容性）

| 变更 | 兼容性 |
| --- | --- |
| 新增 `taifeng.suspend` 包（`SuspendReason` / `PendingRequest` / `SuspensionRecord` / `SuspensionResolver` / `ResolvePlan` / `ResolveError` / `SuspendSignal`） | additive |
| `Op` Union 新增 `Resume`（10 种） | additive；旧 Op 不变 |
| 新增 3 个 EventMsg variant（`turn_suspended` / `suspension_resolved` / `suspension_resolve_rejected`） | additive；旧订阅者可忽略 |
| `TurnCompleted.data["end_reason"]` 新增取值 `"suspended"` | additive；旧业务若硬编码 `{completed,cancelled,error}` 需补一条分支 |
| `PermissionPolicy.preapprove(call_id)`（内部 / resume 用） | additive |
| `SuspendingPrompter` / `request_user_input` 工具 | opt-in；不注入则行为不变 |

### 红线（R1–R5）

| 红线 | 影响 |
| --- | --- |
| **R1 业务零侵入** | `suspend/` 全 typed 无业务词；`detail` / `resolutions` payload 不透明，taifeng 不解析；`created_at` / id 由注入工厂提供（src 内不取系统时钟 / 随机）。 |
| **R2 Cache 友好** | resume 补齐 output 是 tail append（不动 head）；`turn_suspended.cache_invalidated` 标注 tier-2 跨进程必失效。 |
| **R3 可观测** | 新增 3 个 EventMsg；挂起结局经 `TurnCompleted{end_reason="suspended"}`。 |
| **R4 可取消** | 挂起态可被 `CancelTurn` 丢弃（`_cancel_active_suspension`）；协程已退栈不阻塞主 actor。 |
| **R5 可 resume** | `SuspensionRecord` + resolved-marker 走既有 JSONL 追加写；复用 `resume_thread_id` 跨进程重建。 |

### 测试

`tests/test_suspend.py` + 扩 `tests/test_engine_e2e.py`（全走 MockClient）：多挂起点并存、部分 resolution 拒绝、permission allow / deny、form / data 回填、system_retry 自动 retry 耗尽 → 挂起 → resume 重跑、重复 resume 幂等、挂起中 CancelTurn、payload schema 校验、tier-2 跨进程重建。全量套件 668 全绿。

### 文档

- 能力契约 `docs/architecture/capabilities/suspend-resume.md`（新增）+ capabilities README 索引
- `docs/architecture/agent-loop.md` —— 终止结局加 `suspended`、`Resume` Op、续跑数据流
- `docs/configurable-knobs.md` —— `Resume` Op / `SuspendingPrompter` / `request_user_input` / retry-then-suspend / `PermissionPolicy.preapprove`
