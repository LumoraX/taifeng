"""OffloadStrategy 的 R5 可 resume 端到端验证。

链路:offload 落盘 + stub → stub 落 JSONL → 新 store replay 重建 stub →
据确定性路径 file_read 回读 → 内容与 offload 前逐字节一致。

offload 文件独立于 history 持久化:即便 history 仅靠 JSONL stub 重建,落盘原文仍在。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionContext
from taifeng.context.injection import InitialContextInjection
from taifeng.context.placeholders import OFFLOAD_PREFIX
from taifeng.context.strategies import OffloadStrategy
from taifeng.conversation import JsonlMessageStore
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.tool.builtins import make_file_read_tool
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path

TID = "t-resume"
BIG = "\n".join(f"line-{i}-" + "x" * 80 for i in range(400))  # 多行大结果


def _history() -> list[ResponseItem]:
    return [
        user_message("请分析", thread_id=TID),
        function_call("c1", "search", "{}", thread_id=TID),
        function_call_output(call_id="c1", output=BIG, thread_id=TID),
        assistant_message("ok", thread_id=TID, model="m"),
    ]


def _ctx(history: list[ResponseItem]) -> CompressionContext:
    return CompressionContext(
        history=history,
        token_estimate=10_000,
        budget=ContextBudget(context_window=10_000),
        cache_anchor_index=0,
        phase="mid_turn",  # type: ignore[arg-type]
        available_injections=frozenset(InitialContextInjection),
    )


async def test_offload_survives_jsonl_resume(tmp_path: Path) -> None:
    """offload→JSONL→replay→file_read 回读逐字节一致(R5)。"""
    file_root = tmp_path / "files"
    file_root.mkdir()
    store_dir = tmp_path / "store"

    # 1. offload:落盘 + 产出 stub
    strat = OffloadStrategy(file_root=file_root, offload_bytes_threshold=1024)
    result = await strat.compress(_ctx(_history()), InitialContextInjection.DO_NOT_INJECT)
    assert result.success
    stub_item = next(
        it for it in result.new_history if it.kind == "function_call_output"
    )
    assert stub_item.payload["output"].startswith(OFFLOAD_PREFIX)

    # 2. stub 落 JSONL
    store = JsonlMessageStore(store_dir)
    await store.append(stub_item)

    # 3. 新建 store 模拟进程重启后 replay
    resumed_store = JsonlMessageStore(store_dir)
    replayed = [it async for it in await resumed_store.load_thread(TID)]
    replayed_stub = next(it for it in replayed if it.kind == "function_call_output")
    # stub 文本经 JSONL 往返不变
    assert replayed_stub.payload["output"] == stub_item.payload["output"]

    # 4. 据确定性路径 file_read 回读原文(新建的 reader 模拟 resume 后的工具)
    reader = make_file_read_tool(root_dir=file_root)
    rel_path = f"_offload/{TID}/c1"
    ctx = ToolContext(call_id="r1", cancel=CancellationToken(), thread_id=TID)
    r = await reader.handler({"path": rel_path}, ctx)
    assert not r.is_error
    assert r.output == BIG  # 逐字节一致


async def test_offload_recall_paged_after_resume(tmp_path: Path) -> None:
    """resume 后可用 file_read offset/limit 分页回读(不被整文件截断)。"""
    file_root = tmp_path / "files"
    file_root.mkdir()
    strat = OffloadStrategy(file_root=file_root, offload_bytes_threshold=1024)
    await strat.compress(_ctx(_history()), InitialContextInjection.DO_NOT_INJECT)

    reader = make_file_read_tool(root_dir=file_root)
    ctx = ToolContext(call_id="r1", cancel=CancellationToken(), thread_id=TID)
    r = await reader.handler(
        {"path": f"_offload/{TID}/c1", "offset": 0, "limit": 3}, ctx
    )
    assert not r.is_error
    assert r.output == "\n".join(BIG.splitlines()[:3])
