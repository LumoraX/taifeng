"""SurgicalTrimStrategy —— 就地有损剪枝（手术刀档）测试。

覆盖 spec `compaction-surgical-trim` 全部 Requirement：
三 pass（dedup / soft / hard）、可剪窗口与 anchor、glob deny 优先、孤儿跳过、
ratio + cache-TTL 触发、幂等、协作式取消、detail 透出、配对完整性。
"""

from __future__ import annotations

import asyncio

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import (
    CompressionContext,
    CompressionResult,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies.surgical_trim import SurgicalTrimStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)

TID = "t-trim"


def _pair(call_id: str, name: str, output: str) -> list[ResponseItem]:
    """构造一对 function_call + function_call_output。"""
    return [
        function_call(call_id, name, "{}", thread_id=TID),
        function_call_output(call_id=call_id, output=output, thread_id=TID),
    ]


def _history(*, pairs: list[tuple[str, str, str]], tail_msgs: int = 1) -> list[ResponseItem]:
    """user 开场 + 若干 fc/output 对 + 尾部 assistant 消息。"""
    items: list[ResponseItem] = [user_message("请分析", thread_id=TID)]
    for call_id, name, out in pairs:
        items += _pair(call_id, name, out)
    for i in range(tail_msgs):
        items.append(assistant_message(f"tail-{i}", thread_id=TID, model="m"))
    return items


def _ctx(
    history: list[ResponseItem],
    *,
    token_estimate: int = 10_000,
    window: int = 10_000,
    anchor: int = 0,
    phase: str = "pre_turn",
) -> CompressionContext:
    return CompressionContext(
        history=history,
        token_estimate=token_estimate,
        budget=ContextBudget(context_window=window),
        cache_anchor_index=anchor,
        phase=phase,  # type: ignore[arg-type]
        available_injections=frozenset(InitialContextInjection),
    )


def _strategy(**kw: object) -> SurgicalTrimStrategy:
    defaults: dict[str, object] = {
        "priority": 20,
        "soft_trim_ratio": 0.3,
        "hard_clear_ratio": 0.5,
        "min_dedup_chars": 64,
        "head_chars": 40,
        "tail_chars": 20,
        "protect_tail_messages": 1,
    }
    defaults.update(kw)
    return SurgicalTrimStrategy(**defaults)  # type: ignore[arg-type]


def _outputs(history: list[ResponseItem]) -> list[str]:
    return [
        it.payload["output"] for it in history if it.kind == "function_call_output"
    ]


# ───────────────────────── 协议与 detail 字段 ─────────────────────────


def test_compression_result_detail_defaults_empty() -> None:
    """1.1：CompressionResult.detail 新字段默认空 dict，既有构造点零改动兼容。"""
    r = CompressionResult(
        success=False, cache_invalidated=False, anchor_preserved_until=0
    )
    assert r.detail == {}


def test_strategy_satisfies_protocol() -> None:
    """实现 CompressionStrategy 协议（name/priority/should_trigger/compress）。"""
    s = _strategy()
    assert s.name == "surgical_trim"
    assert isinstance(s.priority, int)


# ───────────────────────── 触发条件 ─────────────────────────


def test_should_trigger_below_ratio_returns_none() -> None:
    s = _strategy(soft_trim_ratio=0.3)
    ctx = _ctx([], token_estimate=2_000, window=10_000)  # 20% < 30%
    assert s.should_trigger(ctx) is None


def test_should_trigger_at_ratio_fires() -> None:
    s = _strategy(soft_trim_ratio=0.3)
    ctx = _ctx([], token_estimate=3_500, window=10_000)
    trig = s.should_trigger(ctx)
    assert trig is not None and trig.reason == "token_limit"


def test_cache_ttl_gate_blocks_then_allows() -> None:
    """2.5：ttl 闸——剪枝成功后 ttl 内不再触发；时间推进后恢复。"""
    now = [0.0]
    s = _strategy(cache_ttl_seconds=60.0, clock=lambda: now[0])
    big = "x" * 600
    hist = _history(pairs=[("c1", "read_file", big)])
    ctx = _ctx(hist, token_estimate=4_000)
    assert s.should_trigger(ctx) is not None  # 从未剪过 → 放行

    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))
    assert res.success
    assert s.should_trigger(ctx) is None  # 刚剪过，ttl 内闸住
    now[0] = 61.0
    assert s.should_trigger(ctx) is not None  # ttl 过期恢复


# ───────────────────────── soft-trim ─────────────────────────


def test_soft_trim_truncates_big_output_keeps_pairing() -> None:
    """2.3：soft 档头尾截断 + 省略标记；item 数与 fc 配对不变。"""
    big = "A" * 5_000
    hist = _history(pairs=[("c1", "read_file", big)])
    s = _strategy()
    # ratio 在 soft 与 hard 之间 → 只 soft
    ctx = _ctx(hist, token_estimate=4_000, window=10_000)
    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))

    assert res.success
    assert res.detail["soft_trimmed"] == 1 and res.detail["hard_cleared"] == 0
    assert len(res.new_history) == len(hist)  # 永不删条
    out = _outputs(res.new_history)[0]
    assert "已省略" in out and len(out) < 200
    # 配对的 function_call 原样
    fc = [it for it in res.new_history if it.kind == "function_call"][0]
    assert fc.payload == {"call_id": "c1", "name": "read_file", "arguments": "{}"}


def test_mid_turn_preserves_anchor_bytes() -> None:
    """2.3：DO_NOT_INJECT 下 anchor 之前逐字节不变，cache_invalidated=False。"""
    big = "B" * 5_000
    # anchor 之前也放一对大 output（不可动）
    hist = (
        _pair("c0", "read_file", big)
        + [user_message("u", thread_id=TID)]
        + _pair("c1", "read_file", big)
        + [assistant_message("tail", thread_id=TID, model="m")]
    )
    anchor = 3  # c0 对 + user 在 anchor 前
    s = _strategy()
    ctx = _ctx(hist, token_estimate=4_000, anchor=anchor, phase="mid_turn")
    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))

    assert res.success and res.cache_invalidated is False
    assert res.anchor_preserved_until == anchor
    for i in range(anchor):
        assert res.new_history[i].payload == hist[i].payload  # 逐项不变
    assert res.detail["soft_trimmed"] == 1  # 只有 anchor 后那条被剪


def test_protected_tail_not_trimmed() -> None:
    """2.3：保护尾内的大 output 不剪。"""
    big = "C" * 5_000
    hist = _history(pairs=[("c1", "read_file", big)], tail_msgs=0)
    # output 是最后一条 → protect_tail_messages=1 覆盖它
    s = _strategy(protect_tail_messages=1)
    ctx = _ctx(hist, token_estimate=4_000)
    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))
    assert res.success is False and res.reason == "nothing_to_trim"


# ───────────────────────── hard-clear ─────────────────────────


def test_hard_clear_replaces_with_placeholder() -> None:
    """2.4：ratio 超 hard 阈值 → 占位符整体替换（含原始长度）。"""
    big = "D" * 5_000
    hist = _history(pairs=[("c1", "read_file", big)])
    s = _strategy()
    ctx = _ctx(hist, token_estimate=6_000, window=10_000)  # 60% > hard 0.5
    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))

    assert res.success and res.detail["hard_cleared"] == 1
    out = _outputs(res.new_history)[0]
    assert out.startswith("[pruned:") and "5000" in out


def test_hard_clear_default_not_cross_anchor() -> None:
    """2.4：默认 allow_head_clear=False —— pre_turn 也不越 anchor。"""
    big = "E" * 5_000
    hist = _pair("c0", "read_file", big) + [
        user_message("u", thread_id=TID),
        assistant_message("tail", thread_id=TID, model="m"),
    ]
    s = _strategy()
    ctx = _ctx(hist, token_estimate=6_000, anchor=3, phase="pre_turn")
    res = asyncio.run(
        s.compress(ctx, InitialContextInjection.BEFORE_LAST_USER_MESSAGE)
    )
    assert res.success is False  # 唯一可剪项在 anchor 前 → 无可剪
    assert res.cache_invalidated is False


def test_hard_clear_cross_anchor_marks_invalidated() -> None:
    """2.4：allow_head_clear=True 越 anchor → cache_invalidated=True 如实标注。"""
    big = "F" * 5_000
    hist = _pair("c0", "read_file", big) + [
        user_message("u", thread_id=TID),
        assistant_message("tail", thread_id=TID, model="m"),
    ]
    anchor = 3
    s = _strategy(allow_head_clear=True)
    ctx = _ctx(hist, token_estimate=6_000, anchor=anchor, phase="pre_turn")
    res = asyncio.run(
        s.compress(ctx, InitialContextInjection.BEFORE_LAST_USER_MESSAGE)
    )
    assert res.success and res.detail["hard_cleared"] == 1
    assert res.cache_invalidated is True
    assert res.anchor_preserved_until < anchor


# ───────────────────────── 去重 ─────────────────────────


def test_dedup_keeps_latest_replaces_older() -> None:
    """2.2：同内容三份 → 旧两份换 duplicate 占位符，最新保留完整。"""
    same = "G" * 500
    hist = _history(
        pairs=[("c1", "read_file", same), ("c2", "read_file", same), ("c3", "read_file", same)]
    )
    s = _strategy(soft_trim_ratio=0.9, hard_clear_ratio=0.95)  # 只让 dedup 生效
    ctx = _ctx(hist, token_estimate=500, window=10_000)
    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))

    assert res.success and res.detail["deduped"] == 2
    outs = _outputs(res.new_history)
    assert outs[0].startswith("[duplicate") and outs[1].startswith("[duplicate")
    assert outs[2] == same  # 最新一份完整


def test_dedup_below_min_chars_skipped() -> None:
    """2.2：低于 min_dedup_chars 的重复不参与去重。"""
    small = "h" * 10
    hist = _history(pairs=[("c1", "t", small), ("c2", "t", small)])
    s = _strategy(soft_trim_ratio=0.9, hard_clear_ratio=0.95, min_dedup_chars=64)
    ctx = _ctx(hist, token_estimate=100)
    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))
    assert res.success is False and res.reason == "nothing_to_trim"


# ───────────────────────── glob 与孤儿 ─────────────────────────


def test_glob_deny_priority() -> None:
    """2.1：deny 优先于 allow —— deny 命中的工具不剪，其余照剪。"""
    big = "I" * 5_000
    hist = _history(
        pairs=[("c1", "search_web", big), ("c2", "read_file", big)]
    )
    s = _strategy(deny_globs=("search_*",))
    ctx = _ctx(hist, token_estimate=4_000)
    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))

    assert res.success and res.detail["soft_trimmed"] == 1
    outs = _outputs(res.new_history)
    assert outs[0] == big  # search_web 被 deny 保护
    assert "已省略" in outs[1]


def test_orphan_output_skipped() -> None:
    """2.1：找不到配对 function_call 的 output 视为不可剪，跳过不抛错。"""
    big = "J" * 5_000
    hist = [
        user_message("u", thread_id=TID),
        function_call_output(call_id="ghost", output=big, thread_id=TID),
        assistant_message("tail", thread_id=TID, model="m"),
    ]
    s = _strategy()
    ctx = _ctx(hist, token_estimate=4_000)
    res = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))
    assert res.success is False and res.reason == "nothing_to_trim"


# ───────────────────────── 幂等 ─────────────────────────


def test_idempotent_second_compress_noop() -> None:
    """2.6：剪过的 history 再剪 → success=False, nothing_to_trim，零改写。"""
    big = "K" * 5_000
    hist = _history(pairs=[("c1", "read_file", big), ("c2", "read_file", big)])
    s = _strategy()
    ctx = _ctx(hist, token_estimate=6_000)  # hard 档 + dedup 全开
    first = asyncio.run(s.compress(ctx, InitialContextInjection.DO_NOT_INJECT))
    assert first.success

    ctx2 = _ctx(first.new_history, token_estimate=6_000)
    second = asyncio.run(s.compress(ctx2, InitialContextInjection.DO_NOT_INJECT))
    assert second.success is False and second.reason == "nothing_to_trim"


# ───────────────────────── 取消（协作式检查点）─────────────────────────


def test_cancel_at_pass_boundary() -> None:
    """2.7：pass 边界设 asyncio 检查点 —— 外部 task.cancel 在边界生效。"""

    async def main() -> None:
        big = "L" * 5_000
        hist = _history(pairs=[(f"c{i}", "read_file", big + str(i)) for i in range(50)])
        s = _strategy()
        ctx = _ctx(hist, token_estimate=6_000)
        task = asyncio.ensure_future(
            s.compress(ctx, InitialContextInjection.DO_NOT_INJECT)
        )
        # 先让协程真正启动并跑到它的第一个内部检查点（若无检查点，纯同步
        # 实现会在本次调度片内一口气跑完 → cancel 落空 → await 拿到结果 → 断言失败）
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("compress 无协作检查点：同步跑完未响应取消")

    asyncio.run(main())
