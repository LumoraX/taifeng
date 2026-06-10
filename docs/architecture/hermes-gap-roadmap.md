# 引擎能力对比差距路线图

> 最近更新：2026-06-09（第三轮对比分析：发现 6 项新缺口并立 openspec change；A1 已落地）
> 上一版：2026-05-30（K1–K7 内核子系统全补齐；P0/P1/P2 + K 双清零）
> 上游对比：codex (Rust) / claw-code (Rust) / hermes-agent (Python) / openclaw (TS)
> 本地参照路径：`<opensource>/{codex, claw-code, hermes-agent, openclaw}`
> 用途：下次会话直接 Read 本文，按优先级挑一项开新 change。
>
> **本文视角 = 能力 / 特性完善度**（逐 feature，带 commit 状态）。另有
> `docs/architecture/kernel-gap-analysis.md` 从**微内核子系统/机制**视角看"内核缺哪块"——
> 两文不冲突、互补。**重叠对账**：① 下方 §MemoryProvider 在 kernel-gap 里升格为
> **[K3] 缺失的内核 swap 子系统**（高优先级），以那边定性为准；② 下方 G6 的
> PTY/checkpoint/turn-diff，kernel-gap 判为 **userspace，非内核差距**；③ kernel-gap
> 另列了本文未覆盖的 K1/K2/K4/K5/K6/K7（准入/资源强制/背压/取消竞态/自省/fork 快照）。

## ⚠️ 重要修订（2026-05-29）

**撤回上一版（2026-05-27）"hermes capability gap 已全部闭环"的结论** —— 该结论过于乐观：

1. hermes 近期已落地 **MemoryProvider 协议 / RAG 后端 / 委托隔离 / 后台自改进 loop**，这些当时被列为"开放问题"或漏判，现已是 hermes 已发布子系统（见下方 §R1 边界待决与 §低优先级）。
2. 本次同时纳入 **codex / claw-code / openclaw** 三方对比，差距维度大幅扩展。

**同时，经源码核对，纠正三处审计误报（taifeng 其实已具备，不是差距）：**

| 审计怀疑的"差距" | 核对结论 | 证据 |
| --- | --- | --- |
| 中断 turn 的孤儿 tool_call（function_call 无 output） | **不是 bug**。`_invoke` 把 `CancelledError`/`TimeoutError`/`Exception` 全部吞为 `ToolResult.error` 返回，`function_call_output` 必然追加；`_walk_back_to_safe_boundary` 双向保护切点 | `tool/runtime.py:120-130`、`context/strategies/handoff.py:75-98` |
| 子 skill 派发共享父可变 history | **不是 gap**。`run_sub_skill` 给子 turn 独立 `history_buffer=[seed]` + 新建 sub_thread，上下文隔离干净 | `loop/turn.py:614-648` |
| cache-break 缺 reason code / token_drop | **抽象已有**（`CacheBreakReason` literal + `token_drop` + expected/unexpected）。真实缺口是 **turn 内未自动判定 reason**（snapshot/tool/system 变更都落到 `unknown_drop`），属接线缺口非抽象缺口 | `context/cache_stats.py`、`loop/turn.py:271-303` |

---

## taifeng 已具备（不再列入差距）

skill 派发（atomic/composite + 深度/环检测）、RwLock 并行/独占工具调度、JSONL store + SQLite 旁路 + resume、cache-aware 压缩（handoff + sliding；mid-turn 只动 tail，配对边界保护，**失败时保留历史不截断**）、ModelClient session/turn 拆分 + 多 provider（OpenAI-compat/Anthropic/Gemini/DeepSeek/LiteLLM）+ retry-with-hint、Submission/EventMsg 双总线、hooks（PreToolUse/PostToolUse/PreCompact/PreTurn/Pre|PostSkillDispatch/Pre|PostScriptUse）、PermissionPolicy + HITL（args_match + Claude Code 风格语法）、MCP stdio client + server、structured output、OTel telemetry、父子 cancellation、subagent isolation policy、instruction resolver、SKILL.md 热更 watcher、脚本执行器。

cache-break reason **taxonomy** 已存在但未自动判定 —— 见 G-CACHE。

---

## 待补差距（按优先级 + 来源依据）

### 🔴 P0 — 压缩正确性兜底层（codex + claw-code + openclaw 三方收敛）

taifeng 有好的 handoff prompt（要求保留 ID / 分段）+ 配对边界保护 + 失败保留历史，但**生成之后无任何校验**（仅判 `summary_text.strip()` 非空，`handoff.py:243`）。三家都多一层：

> ✅ **本批已落地（2026-05-29，分支 `feat/compaction-hardening-p0` 接 `feat/compaction-quality-audit`）**：
> G1a / G-CACHE / G1b / G1c / G2b 全部实现并测试通过（详见下方各项「现状」）。

| ID | 能力 | 来源 | taifeng 现状 |
| --- | --- | --- | --- |
| **G1a** | 摘要质量审计 + 有界重生成：必含分段在不在、URL/hash/path/port 等标识符有没有丢、与最新用户问题 token 重叠；不达标则有界重试 | openclaw `src/agents/pi-hooks/compaction-safeguard-quality.ts::auditSummaryQuality` | ✅ commit `f8463c4`（标识符审计 + 有界重生成 + 失败保留历史；分段缺失记为非致命 quality_warnings） |
| **G1b** | 压缩后健康探针：避免把损坏会话喂给模型 | claw-code `crates/runtime/src/conversation.rs:299-334` | ✅ commit `2cbdc44`（**以结构配对完整性校验 + 回滚实现**，非 probe-tool 形式；同目标：压缩引入新孤儿则不应用、保留原 history） |
| **G1c** | 多次压缩降级告警："长线程多次压缩准确率下降，建议开新 thread" | codex `core/src/compact.rs` (`COMPACT_USER_MESSAGE_MAX_TOKENS`) | ✅ commit `2cbdc44`（engine 跨 turn 累计压缩次数，达阈值 emit `CompactionDegradationWarning`） |

**R1 判定：✅ 全部业务零侵入。R2/R4 影响：正面（压缩静默丢标识符是最隐蔽的生产事故）。**

### ✅ P0 — G-CACHE：cache-break reason 自动判定接线（性能 / 可观测）— commit `cc86f24`

reason taxonomy 已在 `cache_stats.py` 定义，但 `turn.py` 仅在压缩后设 `_next_cache_break_reason`。snapshot/tool/system 变更导致的 cache 失效全部落 `unknown_drop`，且 `cache_stats` 每 turn 重置 → 跨 turn drop 检不出。**已实现**：engine 持久 `cache_stats` + prompt 结构指纹跨 turn 传递，变更归因为 `skill_snapshot_changed`/`tool_spec_changed`/`system_prompt_changed`。R1：✅。

### ✅ P1 — G2b：发送前 context budget 预检 + body-size 硬护栏 — commits `2cbdc44` + `bb6015c`

claw-code `crates/api/src/error.rs` 在发请求前估算超限并给"先压缩"指引。**已实现**：① `_sample_once` 发送前估算 token 超 hard limit 即 emit `ContextBudgetExceeded`（非阻塞告警）；② `ContextBudget.max_request_bytes`（opt-in）启用后请求体字节超限在发送前抛 `RequestTooLargeError`（`failure_class=request_size`，确定性、无误判）。R1：✅。

### ✅ P1 — G3：错误分类与韧性（claw-code 领先）— **全部完成**

- ✅ commits `891e12c`/`c5e7989`：稳定 `FailureClass` 字符串桶（11 类）+ 每类 `suggested_action` + `classify_failure(exc)`；`TurnFailed` 透出 `failure_class`/`suggested_action`；OTel `taifeng.turn.failures{failure_class=...}` counter。
- ✅ commit `af8dd2c`：typed 恢复配方 `recommend_recovery(failure_class) → RecoveryPlan{steps, auto_retry_once, escalate}`（机读），随 `TurnFailed.data['recovery']` 透出。**定位：只产出建议、业务侧编排执行（R1）。**
- ✅ commit `aa4cb84`：服务端 request-id 回流（`extract_request_id` 兜底链；openai_compat〔含 deepseek〕/anthropic/gemini 三家 native 捕获，失败回填 `LLMError.request_id`、成功入 `completed`；`TurnFailed` 透出 `request_id`）。**litellm 未接**（SDK 抽象响应头，按需另补）。
- ✅ commit `aa4cb84`：结构化 rate-limit 窗口（`extract_rate_limit_snapshot` 解析 OpenAI/Anthropic `*ratelimit*` 头 → `RateLimitSnapshot`，三家 native 成功时 emit `rate_limits` 事件）。credits/余额属业务（R1 排除）。

### ✅ P2 — G4：skill 可见性治理（openclaw 领先）— 已落地

- ✅ commits `d304171`+`77916e1`：运行时资格门控（`SkillRequirements{bins/env/os}` + `SkillExposure{model_invocable/user_invocable}`；缺 bin/env/os 的 skill 不进 prompt）。**R1 关键**：`src/` 不读 env/PATH，由业务注入 `RuntimeCapabilities`（`skill/eligibility.py`）；engine→TurnRunner→prompt 全链路接线。
- ✅ 曝光维度拆分：`model_invocable=False` 对模型隐藏（不进 `available_child_skills`）。
- ✅ **已具备（核实）**：snapshot 版本号 —— `SkillSnapshot.version` + `FilesystemSkillRegistry._version_counter`（discover/watcher 自增）早已实现。**filter 变更失效 = N/A**：taifeng 无"per-agent 动态 skill filter"概念（用静态 `reachable_from(entry)`），故无此轴。**本项关闭。**

### ✅ P2 — G5：权限 / hooks 细化（claw-code 领先）— 已收口

- ✅ commit `cb17dd1`：**工具 hook 的 args 改写**（PreToolUse 支持 `args_override`，与 script hook 对齐）。
- ✅ commit `c3773f9`（G5d）：**权限能力阶梯** `PermissionPolicy.from_capability_tier(read_only / workspace_write / danger_full_access)` —— 展开为既有 scope 规则的便利构造，契合 taifeng per-builtin 权限模型。
- 🟦 **判为不适用（设计冲突，非待办）**：hook 返回 permission **裁决**喂"中央权限引擎"。**核实结论**：taifeng **按设计做 per-builtin 权限**（IO/network builtin 各自收 `policy`，`call_skill`/`script_exec` 有专属门控），**无中央 tool 权限引擎**。强加中央门 = 改架构 + 与 per-builtin 双重门控冲突。hook 现已能 deny（`allow=False`）——"只能更严不能放宽"本就是安全姿态。若将来要中央门，需先定架构，不作为快速能力补。
- 🟦 **判为已可业务侧实现（非缺口）**：跨子 skill 审批委托。`PermissionRequest.call_chain` 已透传调用栈，业务在 prompter 包装层即可"按 call_chain 记忆一次批准"（permission/types.py 注释已指明 session/DB 级记忆走业务侧）。内核保持无状态（R1）——不在内核做。

### 🟡 P2 — G6：内置工具能力（codex / hermes，均可选 builtin，R1 干净）

> 🔁 **kernel-gap 定性**：下列除"中段截断"外，**PTY exec / turn-diff / checkpoint 均判为
> userspace（非内核差距）**——内置工具属应用层，taifeng 内核只定 `ToolSpec` 协议。
> 故这些不再作为"内核待补"，仅在业务侧按需实现。

- ✅ commit `954aefa`：中段截断 `truncate_middle`（保头尾 + 省略计数），应用于压缩输入 + 工具输出事件预览。**（这是内核侧工具，已做）**
- 🟦 **userspace**：持久 / 交互式 exec（PTY 池，codex `unified_exec`）—— 业务侧工具实现。
- 🟦 **userspace**：turn 级聚合 diff（codex `turn_diff_tracker.rs`）—— 业务侧按需。
- 🟦 **userspace**：checkpoint / 回滚（shadow-git，hermes `checkpoint_manager.py`）—— 业务/应用层职责。

---

## ⚠️ R1 边界待决（别直接开 change，先拍板）

### MemoryProvider 协议 —— ✅ 已落地为 [K3]（commit `1bd57b1`）

> 🔁 **已升格并完成**：从微内核视角它是**内核 swap / 内存层级子系统**（demand-paging），
> 已实现为 `context/memory.py::MemoryStore` 协议（prefetch/writeback/on_pre_evict/
> on_session_end，后端业务实现）。详见 `kernel-gap-analysis.md` [K3]。下文保留 hermes 溯源。

hermes `agent/memory_provider.py` 已证明它**可以 R1-clean**：一个纯协议（`prefetch(query)` 轮前注入 + `sync_turn` 轮后写回 + `on_pre_compress` + `on_session_end` + `on_delegation`），后端（向量/KV，见 `plugins/memory/{holographic,mem0,...}`）全在 plugin 层。taifeng 现在只有 `MessageStore`（短期会话），无长期记忆生命周期钩子。

- 站 R1 严守：RAG 选型 / embedding / 隔离都是业务 → 不做。
- 站能力补全：只暴露协议（prefetch/writeback 生命周期），后端业务实现 —— 与 taifeng "协议层不绑后端"既有风格一致。

**结论更新**：已拍板并落地 —— `context/memory.py::MemoryStore` 协议（`prefetch` / `writeback` / `on_pre_evict` / `on_session_end`），后端业务实现，R1-clean。见 [K3]（commit `1bd57b1`）。本节仅保留 hermes 溯源。

### 其余 hermes 项 —— 多落在 R1 之外

委托 per-child toolset 黑名单（taifeng 已用 `tool_names` + subagent isolation 覆盖，不算缺）、RAG 后端实现、内容安全扫描（`tirith_security`）、Mixture-of-Agents、后台自改进 loop（`background_review.py` / `curator.py`）、planning/todo 原语（`todo_tool.py`，可考虑做成可选 builtin）—— 大多属业务层。

---

## 执行顺序建议

1. ✅ **P0 全部完成**：G1a / G-CACHE / G1b / G1c / G2b（commits `f8463c4` / `cc86f24` / `2cbdc44`）。
2. ✅ **P1 全部完成**：G3 失败分类码 + 恢复配方 + request-id 回流 + rate-limit 窗口（commits `891e12c`/`c5e7989`/`af8dd2c`/`aa4cb84`）；G2b body-size 硬护栏（`bb6015c`）。余项 litellm request-id 按需另补。
3. ✅ **P2 全部收口**：G4 可见性治理（资格门控 + 曝光拆分；snapshot 版本号核实已具备、filter N/A）、G5（a args 改写 + d 能力阶梯；b 中央权限门判设计冲突、c 已可业务侧实现）、G6b 中段截断（commits `d304171`/`77916e1`/`cb17dd1`/`954aefa`/`c3773f9`）。G6 其余项判 userspace。**roadmap 能力缺口至此清零**——余下未做项均为"已具备 / N-A / 业务侧 / 设计冲突"，非待补能力。
4. ✅ **内核子系统 K1–K7 全部补齐**：K1 广度准入（`838265c`）/ K2 资源强制 OOM-killer（`dc633ff`）/
   K3 swap 内存层级（`1bd57b1`，即原 MemoryProvider 升格）/ K4 总线流控（`630c738`）/
   K5 取消终态守卫（`bc09ad9`）/ K6 /proc 自省（`aadced5`）/ K7 谱系持久。详见 `kernel-gap-analysis.md`。
   **能力缺口（P0/P1/P2）与内核子系统（K1–K7）至此双双清零**；后续为可选增强（业务侧 builtin / userspace 工具）。

## 第三轮对比分析（2026-06-09，codex / openclaw / hermes 新提交）

上轮「双清零」后再扫一遍，聚焦**上次清零后上游新增、或之前判 userspace 但可重审**的机制。发现 6 项 R1-clean 缺口，已各立 openspec change（`openspec/changes/<id>/`）：

| change | 缺口 | 优先级 | 状态 |
| --- | --- | --- | --- |
| `reactive-compaction-recovery` | A1 ContextOverflow → 强制压缩 + 单次重采样自愈（替代硬失败丢 turn） | P0 | ✅ **已落地**（`force_compress` + 有界自愈 + `ProviderRetry`；`tests/loop/test_turn_overflow_recovery.py` 全绿） |
| `midturn-input-steering` | B1 运行中 turn 不打断地注入用户输入（codex `inject.rs`/`input_queue.rs`） | P0 | ✅ **已落地**（`InjectUserInput` Op + `pending_input` 共享队列 + `_drain_pending_input` 迭代边界排空 + `UserInputInjected`；`tests/loop/test_midturn_steering.py` 全绿。B1 是 D1 的 seam 底座） |
| `compaction-surgical-trim` | A2 cache-TTL soft/hard 剪枝 + A3 tool-result 去重（handoff 之外更便宜一档） | P1 | ✅ **已落地**（`SurgicalTrimStrategy` 三 pass + `CompressionResult.detail` 透传；`tests/context/test_surgical_trim.py` 17 用例全绿） |
| `turn-resource-guards` | C1 denial 断路器（codex `guardian`）+ C2 IterationBudget 分层/refund（hermes） | P1 | ✅ **已落地**（`DenialBreaker` 单次断路 + `IterationBudget` 子派生独立 + `refunds_iteration`；单元 11 + e2e 3 全绿，行为等价由全量回归守护） |
| `peer-mailbox-messaging` | D1 活体 agent 间 mailbox + 唤醒空闲 + wait-peer（codex `multi_agents_v2`） | P1 | ✅ **已落地**（`deliver_peer_message` 双模式 + `SendToPeer` Op + `send_message`/`wait_peer` 工具 + 4 peer 事件；拓扑路径寻址 deferred） |
| `postcompact-state-reinjection` | E1 压缩后 pinned 状态重注入钩子（hermes todo 穿越压缩） | P1 | ✅ 已落地（todo builtin 范例亦已落地：`TodoStore` + `todo_write`） |

**核实关闭**：C3（子 agent 触发 HITL 阻塞主 actor，hermes `delegate_tool.py` 的 ThreadPoolExecutor 死锁）→ **taifeng 不存在**：单 event-loop async + suspend/resume 释放实例 + `subagent_approval_mode=auto_deny`，无线程模型死锁。
**backlog 处置（按 ADR 0017 四规则裁决）**：A4 多模态重载荷驱逐（规则①，**等真实多模态负载**）、A5 压缩相对增量基线（规则①，低优先）、spawn reject 分类细化（规则①可观测，保留）；E2 ContextEngine 可插拔 slot、E3 prewarm / cancel-reason 带内、peer 拓扑路径寻址（**挂起等需求拉动**）；hermes 侧持久化 todo / 多清单等产品级功能（规则④，**正式关闭**——todo 工作记忆原语已以 `TodoStore` + `todo_write` 落地，规则②）。

## 引用入口

- 本仓库 R1–R5 红线定义：`CLAUDE.md` / `AGENTS.md`
- 当前 capability spec：`capabilities/`
- 架构总览：`docs/architecture/overview.md`
- 业务可配置参数：`docs/configurable-knobs.md`
- 四方参照源码：`<opensource>/{codex, claw-code, hermes-agent, openclaw}`
