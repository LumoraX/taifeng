"""Wave 3 复现:压缩策略对孤儿 output 一律跳过,导致最大的条目压不动。

offload / surgical_trim 都是**就地改写 payload、不删条目**,处理孤儿 output
不可能产生新的配对孤儿；跳过的真实代价是压缩恰恰常把 fc 吃掉、留下大 output。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionContext
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies.offload import OffloadStrategy
from taifeng.context.strategies.surgical_trim import SurgicalTrimStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call_output,
    user_message,
)

TID = "t-orphan"
DNI = InitialContextInjection.DO_NOT_INJECT


def _orphan_history(payload_size: int = 5_000) -> list[ResponseItem]:
    """只有 output、没有配对 function_call（压缩已吃掉 fc）的历史。"""
    return [
        user_message("u", thread_id=TID),
        function_call_output(call_id="gone", output="X" * payload_size, thread_id=TID),
        assistant_message("tail", thread_id=TID, model="m"),
    ]


def _ctx(history: list[ResponseItem], *, anchor: int = -1) -> CompressionContext:
    return CompressionContext(
        history=history, token_estimate=6_000,
        budget=ContextBudget(context_window=10_000),
        cache_anchor_index=anchor, phase="mid_turn",
        available_injections=frozenset(InitialContextInjection),
    )


async def test_offload_handles_orphan_output(tmp_path: Path) -> None:
    """孤儿 output 应正常落盘为 stub（此前被排除 → 压不动）。"""
    strat = OffloadStrategy(file_root=tmp_path, offload_bytes_threshold=1_000)
    hist = _orphan_history()
    assert strat.should_trigger(_ctx(hist)) is not None, "孤儿超阈值应触发"

    result = await strat.compress(_ctx(hist), DNI)
    assert result.success and result.detail["offloaded"] == 1
    out: Any = result.new_history[1].payload["output"]
    assert len(out) < 5_000, "孤儿 output 未被替换为 stub"


async def test_surgical_trims_orphan_under_default_globs() -> None:
    """默认全允许 glob 下孤儿可剪（剪的是 payload，不可能造新孤儿）。"""
    strat = SurgicalTrimStrategy(
        soft_trim_ratio=0.0, hard_clear_ratio=0.9,
        min_dedup_chars=10**6, protect_tail_messages=1,
    )
    result = await strat.compress(_ctx(_orphan_history()), DNI)
    assert result.success, f"默认 glob 下孤儿应可剪，实得 {result.reason}"
    assert result.detail["soft_trimmed"] == 1


async def test_surgical_skips_orphan_when_globs_configured() -> None:
    """配了具体 glob → 无法判定工具名，仍跳过（不猜测）。"""
    strat = SurgicalTrimStrategy(
        soft_trim_ratio=0.0, hard_clear_ratio=0.9,
        min_dedup_chars=10**6, protect_tail_messages=1,
        allow_globs=("read_*",),
    )
    result = await strat.compress(_ctx(_orphan_history()), DNI)
    assert result.success is False and result.reason == "nothing_to_trim"
