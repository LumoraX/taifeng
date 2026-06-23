"""OffloadStrategy —— 无损可回溯压缩档测试。

覆盖 spec `compaction-offload-strategy`:触发(独立 bytes 阈值 / 仅 tool-result /
非 pre_turn)、落盘路径派生、stub 结构、占位符幂等、孤儿跳过、落盘失败保留原文、
R2 只动 tail / cache_invalidated=False、协作式取消。
"""

from __future__ import annotations

from pathlib import Path

import anyio

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionContext
from taifeng.context.injection import InitialContextInjection
from taifeng.context.placeholders import OFFLOAD_PREFIX, is_placeholder
from taifeng.context.strategies.offload import OffloadStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)

TID = "t-offload"
BIG = "x" * 20_000  # 远超默认阈值的大 tool 结果


def _pair(call_id: str, name: str, output: str) -> list[ResponseItem]:
    return [
        function_call(call_id, name, "{}", thread_id=TID),
        function_call_output(call_id=call_id, output=output, thread_id=TID),
    ]


def _history(output: str = BIG, *, call_id: str = "c1") -> list[ResponseItem]:
    """user 开场 + 一对 fc/output + 尾部 assistant。output 默认是大结果。"""
    return [
        user_message("请分析", thread_id=TID),
        *_pair(call_id, "search", output),
        assistant_message("ok", thread_id=TID, model="m"),
    ]


def _ctx(
    history: list[ResponseItem],
    *,
    anchor: int = 0,
    phase: str = "mid_turn",
) -> CompressionContext:
    return CompressionContext(
        history=history,
        token_estimate=10_000,
        budget=ContextBudget(context_window=10_000),
        cache_anchor_index=anchor,
        phase=phase,  # type: ignore[arg-type]
        available_injections=frozenset(InitialContextInjection),
    )


def _strategy(root: Path, **kw: object) -> OffloadStrategy:
    defaults: dict[str, object] = {"file_root": root, "offload_bytes_threshold": 1024}
    defaults.update(kw)
    return OffloadStrategy(**defaults)  # type: ignore[arg-type]


# ---- 触发 ----

def test_should_trigger_on_oversized_result() -> None:
    """tail 中存在超阈值 tool 结果 → 触发。"""
    strat = _strategy(Path("/tmp"))  # noqa: S108 — should_trigger 不落盘
    assert strat.should_trigger(_ctx(_history())) is not None


def test_should_not_trigger_when_all_small() -> None:
    """无超阈值结果 → 不触发。"""
    strat = _strategy(Path("/tmp"))  # noqa: S108
    assert strat.should_trigger(_ctx(_history("small"))) is None


def test_should_not_trigger_pre_turn() -> None:
    """pre_turn 不 offload(spec D5)。"""
    strat = _strategy(Path("/tmp"))  # noqa: S108
    assert strat.should_trigger(_ctx(_history(), phase="pre_turn")) is None


# ---- 落盘 + stub ----

async def test_offload_writes_full_content_to_disk(tmp_path: Path) -> None:
    """超阈值结果完整落盘到 {root}/_offload/{thread_id}/{call_id}。"""
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(_history()), InitialContextInjection.DO_NOT_INJECT)
    assert result.success
    saved = tmp_path / "_offload" / TID / "c1"
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8") == BIG


async def test_offload_replaces_with_stub(tmp_path: Path) -> None:
    """history 中原 output 被替换为 stub:含前缀 / 路径 / call_id / 预览。"""
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(_history()), InitialContextInjection.DO_NOT_INJECT)
    out = next(
        it.payload["output"]
        for it in result.new_history
        if it.kind == "function_call_output"
    )
    assert out.startswith(OFFLOAD_PREFIX)
    assert is_placeholder(out)
    assert "_offload/" in out
    assert "c1" in out


async def test_offload_skips_small_results(tmp_path: Path) -> None:
    """未超阈值不落盘、不改写、success=False。"""
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(_history("small")), InitialContextInjection.DO_NOT_INJECT)
    assert not result.success
    assert not (tmp_path / "_offload").exists()


async def test_offload_skips_non_tool_results(tmp_path: Path) -> None:
    """user/assistant 等非 tool-result 不 offload。"""
    history = [
        user_message("x" * 20_000, thread_id=TID),
        assistant_message("y" * 20_000, thread_id=TID, model="m"),
    ]
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(history), InitialContextInjection.DO_NOT_INJECT)
    assert not result.success


# ---- 幂等 / 孤儿 ----

async def test_offload_idempotent_on_stub(tmp_path: Path) -> None:
    """已 offload 的 stub 不被二次落盘。"""
    strat = _strategy(tmp_path)
    ctx1 = _ctx(_history())
    r1 = await strat.compress(ctx1, InitialContextInjection.DO_NOT_INJECT)
    assert r1.success
    # 用第一轮产物作为新 history 再压一次
    r2 = await strat.compress(_ctx(r1.new_history), InitialContextInjection.DO_NOT_INJECT)
    assert not r2.success


async def test_offload_skips_orphan_output(tmp_path: Path) -> None:
    """无配对 function_call 的孤儿 output 不 offload(不猜测)。"""
    history = [
        user_message("请分析", thread_id=TID),
        function_call_output(call_id="orphan", output=BIG, thread_id=TID),
    ]
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(history), InitialContextInjection.DO_NOT_INJECT)
    assert not result.success


# ---- 失败回退 ----

async def test_offload_write_failure_keeps_original(tmp_path: Path) -> None:
    """落盘失败时保留原始 output,不产生半截 stub。"""
    # 在 thread 目录位置预置一个文件,使 mkdir 失败
    block = tmp_path / "_offload"
    block.mkdir()
    (block / TID).write_text("blocker", encoding="utf-8")  # TID 应是目录,这里占成文件
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(_history()), InitialContextInjection.DO_NOT_INJECT)
    assert not result.success  # 唯一候选落盘失败 → 无成功改写
    # 原 output 不变
    out = next(
        it.payload["output"]
        for it in (result.new_history or _history())
        if it.kind == "function_call_output"
    )
    assert not out.startswith(OFFLOAD_PREFIX)


# ---- R2 ----

async def test_offload_cache_invalidated_false(tmp_path: Path) -> None:
    """offload 只动 anchor 之后的 tail → cache_invalidated=False。"""
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(_history(), anchor=0), InitialContextInjection.DO_NOT_INJECT)
    assert result.success
    assert result.cache_invalidated is False
    assert result.anchor_preserved_until == 0


async def test_offload_detail_counts_for_telemetry(tmp_path: Path) -> None:
    """detail 带 offloaded / bytes_saved 计数(R3:turn 透传进 compaction_completed)。"""
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(_history()), InitialContextInjection.DO_NOT_INJECT)
    assert result.detail["offloaded"] == 1
    assert result.detail["bytes_saved"] == len(BIG.encode("utf-8"))
    assert result.removed_item_count == 1


async def test_offload_does_not_touch_cached_head(tmp_path: Path) -> None:
    """anchor 之前(含)的条目不被 offload,即便超阈值。"""
    # 大 output 在 index 2;把 anchor 设到 2 → 它落在 cached 区,不应 offload
    strat = _strategy(tmp_path)
    result = await strat.compress(_ctx(_history(), anchor=2), InitialContextInjection.DO_NOT_INJECT)
    assert not result.success


# ---- R4 协作取消 ----

async def test_offload_cancel_before_write_skips(tmp_path: Path) -> None:
    """外部 scope 取消 → 在写盘检查点前中断,不落盘。"""
    strat = _strategy(tmp_path)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await strat.compress(_ctx(_history()), InitialContextInjection.DO_NOT_INJECT)
    # 取消在 scope 边界被吞;断言未落盘
    assert not (tmp_path / "_offload" / TID / "c1").exists()
