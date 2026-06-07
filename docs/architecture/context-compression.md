# 上下文压缩

> §1.4 —— Cache-aware 压缩、LLM-to-LLM handoff、`InitialContextInjection` 区分。

**这是 Taifeng 相对 LangGraph / AutoGen / Letta 最大的差异化**。

## 设计目标

1. **不破坏 prompt cache**：mid-turn 压缩只改 tail，保 cached prefix byte-identical
2. **LLM-to-LLM 接力**：压缩不是简单滑窗，而是让另一个 LLM 接班 —— 写 "进度/决策/待办/引用" 四段提示词
3. **可量化失效**：cache break 必须被检测、归因、打点
4. **可叠加多策略**：业务可注册多个 `CompressionStrategy`，按优先级触发

参照（核心范式来源）：
- `codex` `codex-rs/core/src/compact.rs` + `templates/compact/prompt.md`
- `claw-code` `crates/runtime/src/compact.rs` (834 行) + `crates/api/src/prompt_cache.rs` (735 行)
- `openclaw` `pi-embedded-runner/compact.ts` + `SessionCompactionCheckpoint`

不参照：
- `LangGraph` cookbook summarization —— 无 cache 区分
- `另一宿主业务` `compact.py` —— 每次压缩重写 head，cache miss 严重
- `Letta` archival memory —— 单 agent 长期记忆范式，与 turn-based 接力哲学不同

## 核心抽象

```python
# src/taifeng/context/injection.py

from enum import Enum

class InitialContextInjection(Enum):
    """压缩后摘要的注入位置。

    决定了 prompt cache 命中的可能性。
    """

    BEFORE_LAST_USER_MESSAGE = "before_last_user_message"
    """允许动 head。pre-turn 压缩使用。
    
    适用：会话开始前、用户主动 /compact。
    cache 影响：完全失效（next turn 第一次请求重建 cache）。
    """

    DO_NOT_INJECT = "do_not_inject"
    """只改 tail，head 保持 byte-identical。mid-turn 压缩使用。
    
    适用：turn 内 token 溢出、tool 结果过长。
    cache 影响：保持命中（cached prefix 不变）。
    """

    MANUAL_HANDOFF = "manual_handoff"
    """用户显式触发的接力。允许动任意位置，但需要 LLM 重新生成摘要。
    
    适用：用户输入 `/compact` 或 API 调用。
    cache 影响：全部失效。
    """
```

```python
# src/taifeng/context/compressor.py
# 注：三个数据类均为 @dataclass(frozen=True)（不是 pydantic BaseModel）

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable
from taifeng.context.budget import ContextBudget

@dataclass(frozen=True)
class CompressionContext:
    history: list[ResponseItem]
    token_estimate: int
    budget: ContextBudget              # 不再是裸 int —— 含 context_window / soft / hard / max_request_bytes
    cache_anchor_index: int            # 此 index（含）之前的 history 已缓存，mid-turn 不应触碰
    phase: Literal["pre_turn", "mid_turn", "manual"]
    available_injections: frozenset[InitialContextInjection]

@dataclass(frozen=True)
class CompressionTrigger:
    reason: Literal["token_limit", "user_request", "tool_overflow", "scheduled"]
    threshold_pct: float               # 触发时使用率（如 0.85）

@dataclass(frozen=True)
class CompressionResult:
    success: bool
    cache_invalidated: bool            # ← 关键：必须显式声明（mid-turn 应为 False）
    anchor_preserved_until: int        # ← 关键：保 cache 到哪一条之前
    new_history: list[ResponseItem] = field(default_factory=list)  # 压缩后完整 history（保留段 + summary）
    removed_item_count: int = 0
    summary_item_id: str | None = None
    reason: str | None = None          # 失败原因（成功时 None）
    quality_warnings: tuple[str, ...] = ()  # 非致命摘要质量警告（G1a），成功也可能带，供 telemetry

@runtime_checkable
class CompressionStrategy(Protocol):
    name: str
    priority: int                      # 多策略时按优先级排序

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None: ...

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult: ...
```

## 内置策略

### 1. HandoffCompactionStrategy (codex 范式)

```python
# src/taifeng/context/strategies/handoff.py

class HandoffCompactionStrategy:
    """LLM-to-LLM 接力压缩。"""

    name = "handoff"
    priority = 100

    def __init__(self, model_client: ModelClient, model: str | None = None) -> None:
        self._client = model_client
        self._model = model            # None → 用当前对话模型

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        if ctx.token_estimate / ctx.token_budget >= 0.85:
            return CompressionTrigger(reason="token_limit", threshold_pct=0.85)
        return None

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult:
        # 1. 切片：head（保留）+ middle（压缩）+ tail（保留近 N 条）
        keep_tail_n = 4
        compactable_start = ctx.cache_anchor_index if injection == DO_NOT_INJECT else 0
        compactable_end = len(ctx.history) - keep_tail_n

        # 2. 边界保护：tool_use / tool_result 不能切断
        compactable_end = _walk_back_to_safe_boundary(ctx.history, compactable_end)

        if compactable_end - compactable_start < 3:
            return CompressionResult(success=False, reason="too_few_to_compact", ...)

        # 3. 调用 LLM 生成结构化摘要（codex 四段提示词）
        summary = await self._summarize(
            messages=ctx.history[compactable_start:compactable_end],
            template=COMPACT_PROMPT_HANDOFF_ZH,
        )

        # 4. 替换：history[start:end] → [Compacted(summary)]
        # 注入位置由 injection 参数决定
        return CompressionResult(
            success=True,
            cache_invalidated=(injection != DO_NOT_INJECT),
            anchor_preserved_until=compactable_start,
            removed_item_count=compactable_end - compactable_start,
            summary_item_id=...,
        )
```

四段接力提示词（中文化的 codex `templates/compact/prompt.md`）：

```
你正在接手一段被压缩的对话。请基于以下结构化摘要继续。

## 进度 (Progress)
{进行中任务的当前状态、批量操作进度 N/M}

## 决策 (Decisions)
{已做出的决策及理由、已确认的约束}

## 待办 (TODO)
{未完成的任务、用户提出但未回应的请求、已承诺的后续操作}

## 引用 (References)
{所有不可缩写的标识符：UUID / hash / ID / API Key / 文件路径 / 分支名}

---

请直接继续工作。**不要**确认收到摘要，**不要**复述当前进度，**不要**询问用户任何问题。
```

#### G1a 摘要质量审计 + 有界重生成（compaction-hardening P0）

`handoff.py` 在生成摘要后**不直接采信**，先做质量审计再决定是否重生成（构造期 `quality_max_attempts` 控制次数）：

- **必备分段检查**：进度 / 决策 / 待办 / 引用四段在不在（缺失记为非致命 `quality_warnings`，不中断）。
- **标识符保真**：用 `_IDENTIFIER_PATTERNS` 抽取原文里的 URL / hash / path / port 等不可缩写标识符，校验是否在摘要里丢失；丢了就带反馈有界重生成。
- **失败保留历史**：审计始终不达标 / LLM 调用失败时，**不截断、保留原 history**（宁可不压缩也不喂损坏上下文）。
- **健康回滚（G1b）+ 降级告警（G1c）**：压缩若引入新的孤儿 tool 配对则回滚不应用；engine 跨 turn 累计压缩次数达阈值 emit `CompactionDegradationWarning`（建议开新 thread）。

### 2. SlidingWindowStrategy (兜底)

```python
class SlidingWindowStrategy:
    """滑窗 + 图像替换 marker。当 handoff LLM 调用失败时兜底。"""

    name = "sliding"
    priority = 10                      # 最低优先级
```

> 当前 `context/strategies/` **仅导出 `HandoffCompactionStrategy` 与 `SlidingWindowStrategy` 两个策略**。
> 工具结果超长截断走 `context/truncate.py::truncate_middle`（保头尾 + 省略计数，G6b），不是独立 CompressionStrategy。

## tool_use / tool_result 边界保护

参照 claw-code `compact.rs` 的关键发现：

> Walk the boundary back until we start at a safe point. The first preserved
> message is a user message whose first block is a ToolResult, the assistant
> message with the matching ToolUse was slated for removal — that produces an
> orphaned tool role message on the OpenAI-compat path (400: tool message must
> follow assistant with tool_calls).

```python
def _walk_back_to_safe_boundary(history: list[ResponseItem], cut: int) -> int:
    """回退切分点直到不切断 tool_use / tool_result 配对。"""
    while cut > 0 and _would_orphan_tool(history, cut):
        cut -= 1
    return cut

def _would_orphan_tool(history: list[ResponseItem], cut: int) -> bool:
    """检查 history[cut] 是否是孤立的 tool 调用/结果。"""
    if cut >= len(history):
        return False
    item = history[cut]
    if item.kind == "function_call_output":
        # 它的 function_call 必须在 history[:cut] 中
        call_id = item.payload["call_id"]
        return not any(
            h.kind == "function_call" and h.payload["id"] == call_id
            for h in history[:cut]
        )
    return False
```

## Cache break 检测与归因

参照 claw-code `prompt_cache.rs`：

```python
# src/taifeng/context/cache_stats.py

# CacheBreakReason —— 7 类归因 taxonomy（G-CACHE 自动判定接线）
CacheBreakReason = Literal[
    "compaction_pre_turn",                 # 预期内：pre-turn 压缩动 head
    "compaction_manual",                   # 预期内：用户 /compact
    "compaction_mid_turn_anchor_lost",     # ⚠️ mid-turn 压缩本不该破 anchor 却破了
    "skill_snapshot_changed",              # 预期内：skill 列表变更
    "tool_spec_changed",                   # 预期内：工具集变更
    "system_prompt_changed",               # 预期内：system prompt / instructions 变更
    "unknown_drop",                        # ⚠️ 异常：tokens 莫名下降
]

@dataclass
class CacheBreakEvent:
    unexpected: bool
    reason: CacheBreakReason
    previous_cache_read_input_tokens: int
    current_cache_read_input_tokens: int
    token_drop: int

@dataclass                                 # 注：@dataclass，非 BaseModel；Engine 跨 turn 持有一份
class PromptCacheStats:
    completion_cache_hits: int = 0
    completion_cache_misses: int = 0
    expected_invalidations: int = 0        # CompressionResult.cache_invalidated=True 计数
    unexpected_cache_breaks: int = 0       # 没声明却失效了 —— bug 信号
    total_cache_creation_input_tokens: int = 0
    total_cache_read_input_tokens: int = 0
    last_cache_read_input_tokens: int | None = None  # 跨 turn 对比基线
    last_break: CacheBreakEvent | None = None
    history: list[CacheBreakEvent] = field(default_factory=list)
```

每次 LLM 调用返回的 `usage.cache_creation_input_tokens` / `cache_read_input_tokens` 喂给 `PromptCacheStats.record_turn(...)`，对比上一 turn 基线归因。**预期外的 cache break 是 bug**，触发告警。`record_turn` 接收 `anchor_expected` + `anchor_expected_reason`，由 `turn.py` 在压缩 / snapshot / tool / system 变更时设置——避免一切都落 `unknown_drop`（G-CACHE 接线，commit `cc86f24`）。

## 多策略调度

```python
# src/taifeng/context/compressor.py

class CompressionOrchestrator:
    """多策略协调器。

    - 按 priority 倒序尝试
    - 第一个返回 trigger 的策略执行
    - 全部失败返回 OverflowUnrecoverable
    """

    def __init__(self, strategies: list[CompressionStrategy]) -> None:
        self._strategies = sorted(strategies, key=lambda s: -s.priority)

    async def maybe_compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult | None:
        for strat in self._strategies:
            if strat.should_trigger(ctx):
                return await strat.compress(ctx, injection)
        return None
```

## K3 长期记忆 swap 接口（MemoryStore）

压缩把上下文移出窗口后默认**直接丢**（只在 append-only JSONL，不可按需换回）。`context/memory.py::MemoryStore`
补上"内存层级 / demand-paging"这一内核子系统（参照 hermes `memory_provider.py`，剔除业务字段，R1-clean）：

```python
class MemoryStore(Protocol):
    async def prefetch(self, ctx) -> list[ResponseItem]: ...   # turn 前换入（page-in）注入 prompt 尾部
    async def writeback(self, thread_id, items) -> None: ...    # turn 后写回 dirty page
    async def on_pre_evict(self, evicted) -> None: ...          # 压缩换出前抢救 digest，折进保留段（R2 面）
    async def on_session_end(self, thread_id) -> None: ...      # teardown
```

- 默认 `NullMemoryStore`（无内存层级）；engine / pool / TurnRunner（含子 turn）透传，默认 `None`。
- **全 best-effort**：钩子异常不打断 turn。后端（向量 / KV / RAG）全在业务侧，内核只定协议（R1）。
- 详见 `kernel-gap-analysis.md` [K3]，commit `1bd57b1`。

## `replaced_range` 与冷加载消费

`CompressionResult` 的 `replaced_range: tuple[int, int] | None` 字段（以及压缩 salvage note `system_injection(source=memory_pre_evict)`）会被 **`reconstruct_logical_history`**（`conversation/reconstruct.py`）在冷加载 / resume 时消费，把 append-only transcript 折叠回与热内存等价的逻辑 history。

- **写路径不变**：压缩仍 append-only（placeholder 追加末尾，被替换项物理留存）；`replaced_range` 是 `CompressionResult` 的已有字段，只需确保内置策略正确填写。
- **读路径修正**：冷加载 `initial_history` 和 pool resume 均先经 `reconstruct_logical_history`；此前直接读 raw 会把废弃的被压缩项重发给 LLM，现已修正（**既存 resume 隐患被顺带修复**）。
- **自定义策略注意**：若自定义 `CompressionStrategy` 设 `success=True` 但 `summary_item_id=None` 并触发了 salvage note，会产生孤儿 note；`reconstruct` 在此情形下显式校验（不静默误配），作为系统边界。内置 `sliding` / `handoff` 不触发此边界（`summary_item_id` 有值时才追加 salvage）。

## 测试用例（M3 验收）

> 全部已覆盖（`tests/loop/test_compaction_hardening.py` / `test_cache_break_reason.py` / `test_memory_swap.py` 及 `tests/` 下压缩测试）。

- [x] `DO_NOT_INJECT` 模式下，history[:cache_anchor_index] byte-identical 保留
- [x] tool_use/tool_result 配对永不被切断（随机切点验证）
- [x] handoff 摘要标识符审计 + 缺失则有界重生成（G1a）
- [x] 多策略按 priority 顺序触发
- [x] cache_break 检测：预期内 `expected_invalidations++`，预期外 `unexpected_cache_breaks++` 并告警
- [x] mid-turn 压缩失败时保留原 history（不截断），SlidingWindow 兜底
- [x] 压缩 thread 冷加载：`reconstruct_logical_history` 正确消费 `replaced_range`，resume 后 LLM 不再收到废弃项（`tests/conversation/test_reconstruct.py`）
