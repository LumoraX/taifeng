"""ConsoleSink —— 类似 claw-code / codex 的人类可读控制台输出。

样式：
    [12:34:56] turn  → submission_id (entry=code-reviewer)
    [12:34:56] llm   ← assistant: "正在分析..."
    [12:34:56] tool  → call_skill(style-checker) call_id=tc_abc
    [12:34:57] skill ⇣ depth=1 path=[code-reviewer]
    [12:34:58] tool  ← call_skill ok (123ms)
    [12:34:58] turn  ✓ iter=2 dur=1.23s tokens=1234 cached=890 (72%)
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, TextIO

from taifeng.loop.engine import AgentEngine
from taifeng.loop.event import EventMsg


class _Colors:
    RESET = "\x1b[0m"
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"
    BLUE = "\x1b[34m"
    CYAN = "\x1b[36m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    MAGENTA = "\x1b[35m"
    GRAY = "\x1b[90m"


_KIND_TAG = {
    "turn_started": ("turn", _Colors.BLUE, "→"),
    "assistant_text": ("llm ", _Colors.CYAN, "←"),
    "assistant_reasoning": ("llm ", _Colors.MAGENTA, "·"),
    "tool_call_started": ("tool", _Colors.YELLOW, "→"),
    "tool_call_completed": ("tool", _Colors.YELLOW, "←"),
    "skill_dispatched": ("skil", _Colors.MAGENTA, "⇣"),
    "skill_returned": ("skil", _Colors.MAGENTA, "⇡"),
    # 认知回路 ⑦ 沉淀：子 skill 终态战绩记账（R3 审计补：此前落 `?` 兜底）。
    # 灰色 ▦ —— 旁路记账（不进 LLM 视图），与 skill 派发/回流的流程事件区分
    "skill_outcome_recorded": ("skil", _Colors.GRAY, "▦"),
    # 相位 2 skill 发现/召回：检索发起 ⌕ / 候选返回 ▤（蓝色 —— 信息流，非拦截）
    "skill_search_invoked": ("srch", _Colors.BLUE, "⌕"),
    "skill_candidates_returned": ("cand", _Colors.BLUE, "▤"),
    # 召回后精验：保留/滤掉计数（蓝色 ▣ —— 信息流，区分召回 ▤）
    "skill_candidates_verified": ("vrfy", _Colors.BLUE, "▣"),
    "orchestration_plan_resolved": ("plan", _Colors.MAGENTA, "≡"),
    "orchestration_condition_missing": ("plan", _Colors.RED, "✗"),
    "tool_batch_dispatched": ("tool", _Colors.YELLOW, "⇉"),
    "thread_resumed": ("eng ", _Colors.GRAY, "↻"),
    "compaction_started": ("comp", _Colors.GRAY, "→"),
    "compaction_completed": ("comp", _Colors.GRAY, "←"),
    "pinned_state_reinjected": ("comp", _Colors.GRAY, "📌"),
    "budget_hint_injected": ("comp", _Colors.GRAY, "🪙"),
    "cache_break_detected": ("cach", _Colors.RED, "!"),
    # Permission gate 事件（红色 —— 表示拦截）
    "permission_prompt_timeout": ("perm", _Colors.RED, "⏱"),
    "skill_dispatch_hook_denied": ("hook", _Colors.RED, "✗"),
    "skill_dispatch_permission_denied": ("perm", _Colors.RED, "✗"),
    # turn-resource-guards：连续拒绝断路器触发（红色 —— turn 提前终止）
    "denial_circuit_open": ("perm", _Colors.RED, "⊘"),
    "doom_loop_warned": ("loop", _Colors.YELLOW, "↻"),
    "doom_loop_circuit_open": ("loop", _Colors.RED, "⊘"),
    "peer_message_sent": ("peer", _Colors.CYAN, "✉"),
    "peer_agent_woken": ("peer", _Colors.CYAN, "⏰"),
    "peer_wait_started": ("peer", _Colors.GRAY, "⧖"),
    "peer_wait_resolved": ("peer", _Colors.GRAY, "⧗"),
    "turn_completed": ("turn", _Colors.GREEN, "✓"),
    "turn_failed": ("turn", _Colors.RED, "✗"),
    # turn_suspended 是独立终结态(挂起等待 Resume)——黄色 ⏸ 与完成/失败区分
    "turn_suspended": ("turn", _Colors.YELLOW, "⏸"),
    # turn-rewind 回访节点录制（R3 审计补：此前落 `?` 兜底渲染）
    "rewind_checkpoint_recorded": ("rwnd", _Colors.GRAY, "⊙"),
    "rewind_rejected": ("rwnd", _Colors.RED, "⊘"),
    "turn_rewound": ("rwnd", _Colors.GREEN, "↺"),
    # detached-spawn 生命周期 + join-barrier（R3 审计补：真实回归发现全族缺渲染）
    "spawn_started": ("spwn", _Colors.MAGENTA, "⇣"),
    "spawn_suspended": ("spwn", _Colors.YELLOW, "⏸"),
    "spawn_completed": ("spwn", _Colors.GREEN, "✓"),
    "spawn_failed": ("spwn", _Colors.RED, "✗"),
    "spawn_cancelled": ("spwn", _Colors.GRAY, "⊘"),
    "join_barrier_registered": ("join", _Colors.MAGENTA, "⧉"),
    "join_barrier_fired": ("join", _Colors.GREEN, "⚡"),
    # 挂起核销与资源强制（同上）
    "suspension_resolved": ("turn", _Colors.GREEN, "▶"),
    "suspension_resolve_rejected": ("turn", _Colors.RED, "⊘"),
    "resource_limit_exceeded": ("knob", _Colors.RED, "⛔"),
    # midturn-input-steering：运行中注入用户输入（R3 审计补：此前落 `evt` 兜底）
    "user_input_injected": ("inje", _Colors.CYAN, "↘"),
    # post-turn-hook：root turn 真终态收尾审计点（R3 审计补：此前落 `evt` 兜底）
    "post_turn_hook_fired": ("hook", _Colors.GRAY, "⊛"),
    "engine_log": ("eng ", _Colors.GRAY, "·"),
    "shutdown": ("eng ", _Colors.GRAY, "⏹"),
}


def _now_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def _fmt_event(ev: EventMsg, *, color: bool = True, text_buffer: dict[str, str] | None = None) -> str:
    tag, col, arrow = _KIND_TAG.get(ev.msg.kind, ("evt ", _Colors.GRAY, "·"))
    data: dict[str, Any] = ev.msg.data
    parts: list[str] = []

    if ev.msg.kind == "turn_started":
        parts.append(f"sub={ev.submission_id[:12]}")
        if eid := data.get("entry_skill_id"):
            parts.append(f"entry={eid}")
        if model := data.get("model"):
            parts.append(f"model={model}")
    elif ev.msg.kind == "assistant_text":
        delta = data.get("delta", "")
        # 累积 buffer per submission；遇到换行或 turn_completed 才打印
        if text_buffer is not None:
            buf = text_buffer.get(ev.submission_id, "")
            buf += delta
            if "\n" in delta or len(buf) > 120:
                # flush
                line, _, rest = buf.rpartition("\n") if "\n" in buf else (buf, "", "")
                text_buffer[ev.submission_id] = rest
                parts.append(repr(line)[1:-1])  # 去掉引号
            else:
                text_buffer[ev.submission_id] = buf
                return ""  # 暂不打印
        else:
            parts.append(repr(delta)[1:-1])
    elif ev.msg.kind == "assistant_reasoning":
        delta = data.get("delta", "")
        parts.append(f"reasoning: {delta[:80]}")
    elif ev.msg.kind == "tool_call_started":
        parts.append(f"{data.get('name')}({_short(data.get('arguments'), 60)}) call_id={data.get('call_id', '')[:8]}")
    elif ev.msg.kind == "tool_call_completed":
        status = "err" if data.get("is_error") else "ok"
        parts.append(
            f"{data.get('name')} {status} ({data.get('duration_ms', 0)}ms): "
            f"{_short(data.get('output', ''), 80)}"
        )
    elif ev.msg.kind == "skill_dispatched":
        parts.append(f"{data.get('skill_id')} depth={data.get('depth')} path={data.get('stack_path')}")
    elif ev.msg.kind == "skill_returned":
        status = "✓" if data.get("success") else "✗"
        parts.append(f"{data.get('skill_id')} {status} summary={_short(data.get('summary', ''), 60)}")
    elif ev.msg.kind == "skill_outcome_recorded":
        # 战绩记账：skill_id / 战绩 / 信号来源 / 选择来源 / 成本（token + 迭代）
        parts.append(
            f"{data.get('skill_id')} outcome={data.get('outcome')}"
            f" via={data.get('outcome_signal_source')} origin={data.get('selection_origin')}"
            f" cost(tok={data.get('cost_tokens')},it={data.get('cost_iterations')})"
        )
    elif ev.msg.kind == "skill_search_invoked":
        # 召回检索发起：查询文本 + 请求候选上限 + 被检索池规模
        parts.append(
            f"query={_short(data.get('query', ''), 40)!r} "
            f"top_k={data.get('top_k')} pool={data.get('pool_size')}"
        )
    elif ev.msg.kind == "skill_candidates_returned":
        # 召回结果：返回候选数 + 候选 skill_id 列表（按相关度排序）
        parts.append(f"count={data.get('count')} top_ids={data.get('top_ids')}")
    elif ev.msg.kind == "skill_candidates_verified":
        # 精验结果：验证通过保留数 + 被滤掉数（描述像但输入要求不满足 / 无 body 的误召）
        parts.append(
            f"verified={data.get('verified_count')} dropped={data.get('dropped_count')}"
        )
    elif ev.msg.kind == "compaction_started":
        parts.append(f"phase={data.get('phase')} strategy={data.get('strategy')} tokens={data.get('token_estimate')}")
    elif ev.msg.kind == "compaction_completed":
        status = "✓" if data.get("success") else "✗"
        parts.append(
            f"{status} removed={data.get('removed_count')} cache_invalid={data.get('cache_invalidated')}"
            + (f" reason={data.get('reason')}" if data.get("reason") else "")
        )
    elif ev.msg.kind == "peer_message_sent":
        down = " (downgraded)" if data.get("mode_downgraded") else ""
        parts.append(
            f"{data.get('from')} → {data.get('to')} mode={data.get('mode')}"
            f" via={data.get('delivered_via')}{down} len={data.get('text_len')}"
        )
    elif ev.msg.kind == "peer_agent_woken":
        parts.append(f"thread={data.get('thread_id')} handle={data.get('handle_id')}")
    elif ev.msg.kind == "peer_wait_started":
        parts.append(f"handle={data.get('handle_id')} timeout={data.get('timeout_seconds')}s")
    elif ev.msg.kind == "peer_wait_resolved":
        parts.append(
            f"handle={data.get('handle_id')} outcome={data.get('outcome')}"
            f" status={data.get('status')}"
        )
    elif ev.msg.kind == "pinned_state_reinjected":
        names = ",".join(s.get("name", "?") for s in data.get("sources", []))
        parts.append(
            f"phase={data.get('phase')} sources=[{names}] "
            f"total={data.get('total_chars')}ch"
            + (f" dropped={data.get('dropped')}" if data.get("dropped") else "")
        )
    elif ev.msg.kind == "budget_hint_injected":
        parts.append(
            f"used={data.get('used')} window={data.get('context_window')} "
            f"ratio={data.get('ratio')} rem_to_hard={data.get('remaining_to_hard')}"
        )
    elif ev.msg.kind in ("doom_loop_warned", "doom_loop_circuit_open"):
        parts.append(
            f"tool={data.get('tool')!r} consecutive={data.get('consecutive')} "
            f"threshold={data.get('threshold')}"
        )
    elif ev.msg.kind == "cache_break_detected":
        u = "unexpected" if data.get("unexpected") else "expected"
        parts.append(f"{u} drop={data.get('token_drop')} reason={data.get('reason')}")
    elif ev.msg.kind == "permission_prompt_timeout":
        # 业务侧若未配 timeout 永远不会出现；触发即代表"prompter 卡死"诊断信号
        parts.append(
            f"{data.get('scope')}:{data.get('target')} "
            f"after {data.get('timeout_seconds')}s "
            f"chain={data.get('call_chain')}"
        )
    elif ev.msg.kind == "skill_dispatch_hook_denied":
        parts.append(
            f"pre_skill_dispatch → {data.get('target_skill_id')} "
            f"caller={data.get('caller_skill_id')} "
            f"reason={_short(data.get('hook_reason', ''), 60)}"
        )
    elif ev.msg.kind == "skill_dispatch_permission_denied":
        parts.append(
            f"{data.get('caller_skill_id')} → {data.get('target_skill_id')} "
            f"reason={_short(data.get('reason', ''), 60)}"
        )
    elif ev.msg.kind == "turn_completed":
        usage = data.get("usage") or {}
        in_t = usage.get("input_tokens", 0)
        cache_r = usage.get("cache_read_input_tokens", 0)
        ratio = (cache_r / in_t * 100) if in_t > 0 else 0
        parts.append(
            f"iter={data.get('iterations')} dur={data.get('duration_ms')}ms "
            f"in={in_t} out={usage.get('output_tokens', 0)} cached={cache_r} ({ratio:.0f}%)"
            f" end={data.get('end_reason')}"
        )
    elif ev.msg.kind == "turn_failed":
        parts.append(f"error={data.get('error')} kind={data.get('kind')}")
    elif ev.msg.kind == "tool_batch_dispatched":
        # 并发批次派发：count 本批 tool 数，max_parallel 当前并发上限（=1 表串行）
        parts.append(f"batch count={data.get('count')} max_parallel={data.get('max_parallel')}")
    elif ev.msg.kind == "orchestration_plan_resolved":
        # 声明式编排计划解析：把 groups 摘要成 "parallel[a,b] → serial[c] → when(cond)"
        seq = []
        for g in data.get("groups", []) or []:
            gtype = g.get("type")
            if gtype == "when":
                seq.append(f"when({g.get('condition')})")
            else:
                seq.append(f"{gtype}[{','.join(g.get('skills', []))}]")
        parts.append(f"{data.get('skill_id')}: " + " → ".join(seq))
    elif ev.msg.kind == "orchestration_condition_missing":
        parts.append(f"condition missing: {data.get('condition')} (skill={data.get('skill_id')})")
    elif ev.msg.kind == "thread_resumed":
        parts.append(f"thread={data.get('thread_id')} items={data.get('item_count', data.get('items', '?'))}")
    elif ev.msg.kind == "user_input_injected":
        # delivered=True 投进活跃 turn pending；False 无活跃 turn（落史未起新 turn）
        parts.append(f"delivered={data.get('delivered')}")
        if preview := data.get("text_preview"):
            parts.append(_short(preview, 60))
    elif ev.msg.kind == "post_turn_hook_fired":
        parts.append(
            f"end={data.get('end_reason')} iter={data.get('iteration')} "
            f"hooks={data.get('hook_count')}"
        )
    elif ev.msg.kind == "engine_log":
        parts.append(f"{data.get('level')}: {data.get('message')}")
    else:
        # 兜底：没有专用 formatter 的事件（多为诊断/恢复类边缘事件）也必须自描述——
        # 打出 kind 名 + 原始 data，确保"所有关键信息都输出"，不出现匿名 `?` 行。
        parts.append(f"{ev.msg.kind} {data}")

    body = " ".join(parts)
    ts = _now_str()
    if color:
        return f"{_Colors.GRAY}[{ts}]{_Colors.RESET} {col}{tag}{_Colors.RESET} {arrow} {body}"
    return f"[{ts}] {tag} {arrow} {body}"


def _short(s: Any, n: int) -> str:
    s = str(s)
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


class ConsoleSink:
    """订阅 EventMsg 并打印到控制台。

    用法：
        sink = ConsoleSink(out=sys.stdout)
        task = asyncio.create_task(sink.attach(engine))
    """

    def __init__(
        self,
        *,
        out: TextIO | None = None,
        color: bool = True,
        flush_text_on_turn_end: bool = True,
    ) -> None:
        self._out = out or sys.stdout
        self._color = color and self._out.isatty()
        self._flush_text_on_turn_end = flush_text_on_turn_end
        self._text_buffer: dict[str, str] = {}

    async def handle(self, ev: EventMsg) -> None:
        line = _fmt_event(ev, color=self._color, text_buffer=self._text_buffer)
        if line:
            self._out.write(line + "\n")
            self._out.flush()
        # turn 结束时强制 flush 剩余 buffer。turn_suspended 同为终结态(挂起等待 Resume)，
        # 必须纳入，否则挂起前已 buffer 的助手文本会被静默丢弃。
        if self._flush_text_on_turn_end and ev.msg.kind in (
            "turn_completed", "turn_failed", "turn_suspended",
        ):
            rest = self._text_buffer.pop(ev.submission_id, "")
            if rest.strip():
                tag, col, arrow = _KIND_TAG["assistant_text"]
                ts = _now_str()
                prefix = (
                    f"{_Colors.GRAY}[{ts}]{_Colors.RESET} {col}{tag}{_Colors.RESET} {arrow} "
                    if self._color
                    else f"[{ts}] {tag} {arrow} "
                )
                self._out.write(prefix + rest + "\n")
                self._out.flush()

    async def attach(self, engine: AgentEngine) -> None:
        async for ev in engine.subscribe_all():
            await self.handle(ev)


def attach_console_sink(engine: AgentEngine, **kwargs: Any) -> asyncio.Task[None]:
    """便捷：构造 ConsoleSink 并启动后台 task。"""
    sink = ConsoleSink(**kwargs)
    return asyncio.create_task(sink.attach(engine))
