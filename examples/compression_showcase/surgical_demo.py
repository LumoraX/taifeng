"""SurgicalTrim demo —— 手术刀档就地剪枝（dedup / soft / hard 三 pass，无需 key）。

与同目录 demo.py（sliding 整条丢弃）、real_llm/capability_matrix.py（handoff LLM 摘要）
互为三档压缩谱系对比。本 demo 直接在 strategy 层展示三 pass 的前后效果与 detail 计数：

1. **dedup**：同一文件被 read 三次 → 旧两份换 duplicate 占位符，最新保留完整；
2. **soft-trim**（ratio 0.3–0.5）：大 tool output 头尾截断 + 省略标记；
3. **hard-clear**（ratio ≥ 0.5）：整体替换为含原始长度的占位符。

全程 LLM-free、只改写 function_call_output payload、永不删条（fc/output 配对不变）。
契约：docs/architecture/capabilities/compaction-surgical-trim.md。

业务装配（engine 级）只需把策略加进 compressors（推荐最高优先级——最便宜先试）：

    pool = await taifeng.EnginePool.create(
        ...,
        compressors=[SurgicalTrimStrategy(priority=20), HandoffCompactionStrategy(...)],
    )

运行：
    PYTHONPATH=src uv run python examples/compression_showcase/surgical_demo.py
"""

from __future__ import annotations

import asyncio

from taifeng import SurgicalTrimStrategy
from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import CompressionContext
from taifeng.context.injection import InitialContextInjection
from taifeng.conversation.models import (
    ResponseItem,
    function_call,
    function_call_output,
    user_message,
)

TID = "demo"


def _pair(call_id: str, name: str, output: str) -> list[ResponseItem]:
    """一对 fc/output（剪枝只动 output payload，fc 永远原样）。"""
    return [
        function_call(call_id, name, "{}", thread_id=TID),
        function_call_output(call_id=call_id, output=output, thread_id=TID),
    ]


def _show(title: str, history: list[ResponseItem]) -> None:
    """打印各 output 的长度与首 60 字预览。"""
    print(f"\n── {title} ──")
    for it in history:
        if it.kind != "function_call_output":
            continue
        out = it.payload["output"]
        print(f"  [{it.payload['call_id']}] {len(out):>5} 字 | {out[:60]!r}")


async def main() -> None:
    """同一 history 在 soft 档与 hard 档下的剪枝对比。"""
    same_file = "def handler(request):\n    ...  # 同一文件内容\n" * 80
    big_log = "INFO worker heartbeat ok\n" * 200
    history: list[ResponseItem] = [user_message("帮我排查这个服务", thread_id=TID)]
    # 同一文件被反复 read 三次（dedup 目标）+ 一份大日志（trim 目标）
    history += _pair("c1", "read_file", same_file)
    history += _pair("c2", "read_file", same_file)
    history += _pair("c3", "read_file", same_file)
    history += _pair("c4", "shell_exec", big_log)
    history += [user_message("继续", thread_id=TID)]

    def ctx(token_estimate: int) -> CompressionContext:
        return CompressionContext(
            history=history,
            token_estimate=token_estimate,
            budget=ContextBudget(context_window=10_000),
            cache_anchor_index=0,
            phase="pre_turn",
            available_injections=frozenset(InitialContextInjection),
        )

    _show("剪枝前", history)

    # ── soft 档（ratio 0.4：dedup + 头尾截断）──
    soft = SurgicalTrimStrategy(min_dedup_chars=256, head_chars=80, tail_chars=40,
                                protect_tail_messages=1)
    res = await soft.compress(ctx(4_000), InitialContextInjection.DO_NOT_INJECT)
    _show("soft 档剪枝后（ratio=0.4）", res.new_history)
    print(f"  detail = {res.detail}  cache_invalidated = {res.cache_invalidated}")

    # ── hard 档（ratio 0.6：dedup + 整体占位符）──
    hard = SurgicalTrimStrategy(min_dedup_chars=256, protect_tail_messages=1)
    res2 = await hard.compress(ctx(6_000), InitialContextInjection.DO_NOT_INJECT)
    _show("hard 档剪枝后（ratio=0.6）", res2.new_history)
    print(f"  detail = {res2.detail}  cache_invalidated = {res2.cache_invalidated}")

    ok = (
        res.detail == {"deduped": 2, "soft_trimmed": 2, "hard_cleared": 0}
        and res2.detail == {"deduped": 2, "soft_trimmed": 0, "hard_cleared": 2}
    )
    print("\n" + "=" * 60)
    print(f"==> 三 pass 分级剪枝{'确证 ✅' if ok else '与预期不符 ❌'}"
          "（LLM-free、配对不变、cache anchor 不破）")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
