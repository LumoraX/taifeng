"""OffloadStrategy 接入 CompressionOrchestrator 的集成测试。

覆盖 spec `compaction-offload-strategy`:
- offload(priority 30)在有超阈值大结果时优先于 SurgicalTrim(priority 20)被选中
- 未注入 OffloadStrategy 时谱系行为与现状一致(SurgicalTrim 照常工作)
- 从公共包可导入 OffloadStrategy
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.context import OffloadStrategy as OffloadFromContext
from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionContext, CompressionOrchestrator
from taifeng.context.injection import InitialContextInjection
from taifeng.context.placeholders import OFFLOAD_PREFIX
from taifeng.context.strategies import OffloadStrategy, SurgicalTrimStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)

if TYPE_CHECKING:
    from pathlib import Path

TID = "t-int"
BIG = "x" * 20_000


def _history() -> list[ResponseItem]:
    return [
        user_message("请分析", thread_id=TID),
        function_call("c1", "search", "{}", thread_id=TID),
        function_call_output(call_id="c1", output=BIG, thread_id=TID),
        assistant_message("ok", thread_id=TID, model="m"),
    ]


def _ctx() -> CompressionContext:
    # ratio=1.0 → 同时满足 offload 阈值与 surgical_trim soft 阈值
    return CompressionContext(
        history=_history(),
        token_estimate=10_000,
        budget=ContextBudget(context_window=10_000),
        cache_anchor_index=0,
        phase="mid_turn",  # type: ignore[arg-type]
        available_injections=frozenset(InitialContextInjection),
    )


def test_public_export_is_same_class() -> None:
    """taifeng.context 导出的 OffloadStrategy 与 strategies 包内同一个类。"""
    assert OffloadFromContext is OffloadStrategy


async def test_offload_wins_over_surgical_trim(tmp_path: Path) -> None:
    """同时满足两档阈值时,priority 更高的 offload 被 orchestrator 选中(无损优先)。"""
    orch = CompressionOrchestrator(
        [
            SurgicalTrimStrategy(priority=20, soft_trim_ratio=0.3),
            OffloadStrategy(file_root=tmp_path, priority=30, offload_bytes_threshold=1024),
        ]
    )
    result = await orch.maybe_compress(_ctx(), InitialContextInjection.DO_NOT_INJECT)
    assert result is not None and result.success
    # 落盘文件存在 → 确实走了 offload 而非 trim
    assert (tmp_path / "_offload" / TID / "c1").is_file()
    out = next(
        it.payload["output"]
        for it in result.new_history
        if it.kind == "function_call_output"
    )
    assert out.startswith(OFFLOAD_PREFIX)


async def test_without_offload_surgical_trim_still_runs(tmp_path: Path) -> None:
    """未注入 offload 时,SurgicalTrim 照常触发(谱系行为与现状一致)。"""
    orch = CompressionOrchestrator(
        [
            SurgicalTrimStrategy(
                priority=20,
                soft_trim_ratio=0.3,
                head_chars=40,
                tail_chars=20,
                protect_tail_messages=1,  # 4 条 history,留 1 条尾保护使 output 可剪
            )
        ]
    )
    result = await orch.maybe_compress(_ctx(), InitialContextInjection.DO_NOT_INJECT)
    assert result is not None and result.success
    # 未落盘(无 offload),且大结果被 trim 截断
    assert not (tmp_path / "_offload").exists()
    out = next(
        it.payload["output"]
        for it in result.new_history
        if it.kind == "function_call_output"
    )
    assert not out.startswith(OFFLOAD_PREFIX)
    assert len(out) < len(BIG)
