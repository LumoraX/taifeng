"""overflow 有界自愈 demo —— provider 判超长时不丢 turn（MockClient 无需 key）。

演示「A1 reactive-compaction-recovery」兜底路径：当本地 token 估算偏低、但 provider
已判「上下文超长」抛 `ContextOverflowError` 时，taifeng 不直接硬失败丢整个 turn，而是
**有界自愈**——强制压缩一次（绕 should_trigger，phase=overflow）+ 重采样一次，成功则
turn 正常继续；仍失败才硬失败。

时间轴关键事件：`provider_retry` + phase=overflow 的 `compaction_started/completed`。
本 demo 用 MockClient 在第 3 个 turn 的首次采样抛 overflow，自愈后重采样成功。

参照 openclaw pi-embedded-subscribe.ts 的 pendingCompactionRetry。
契约：docs/architecture/capabilities/reactive-compaction-recovery.md。

运行：
    PYTHONPATH=src uv run python examples/compression_showcase/overflow_demo.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.llm.client import ModelClient
from taifeng.llm.errors import ContextOverflowError
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    server_model,
    text_delta,
)
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import ApiRequest, TokenUsage
from taifeng.telemetry import attach_console_sink

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from taifeng.loop.cancellation import CancellationToken

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


class _OverflowSession:
    """单次采样会话：fail=True 时在 stream 首步抛 ContextOverflowError。"""

    def __init__(self, *, fail: bool, cancel: CancellationToken) -> None:
        self._fail = fail
        self._cancel = cancel

    async def __aenter__(self) -> _OverflowSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """fail 时首步抛 overflow；否则正常回放一段短文本。"""
        self._cancel.raise_if_cancelled()
        if self._fail:
            raise ContextOverflowError("provider: context length exceeded")
        yield created()
        yield server_model("mock-model")
        yield text_delta("收到，已基于压缩后的上下文继续作答。")
        yield completed(
            response_id="r",
            usage=TokenUsage(input_tokens=50, output_tokens=20),
            end_turn=True,
        )


class _OverflowOnNthClient(ModelClient):
    """第 ``fail_on`` 次采样抛 ContextOverflowError，其余正常。

    用于精确地在「第 3 个 turn 的首次采样」逼出 provider overflow：前两轮 warmup
    （call 1、2）正常累积 history，第 3 轮首采样（call 3）overflow → 自愈重采样（call 4）。
    """

    def __init__(self, *, fail_on: int) -> None:
        self._calls = 0
        self._fail_on = fail_on

    @property
    def calls(self) -> int:
        return self._calls

    def session(
        self, *, cancel: CancellationToken, model: str | None = None
    ) -> _OverflowSession:
        self._calls += 1
        return _OverflowSession(fail=self._calls == self._fail_on, cancel=cancel)


def _summary_strategies() -> list[HandoffCompactionStrategy]:
    """handoff 压缩策略：摘要由独立 MockClient 提供（force_compress 时调用）。

    返回策略列表（EnginePool.create 的 ``compressors=`` 内部自行包 Orchestrator）。
    """
    summary_client = MockClient(
        turns=[
            MockTurn(
                text="## 进度摘要\n前序对话已归纳为要点，继续推进。",
                usage=TokenUsage(input_tokens=400, output_tokens=20),
            )
            for _ in range(3)
        ]
    )
    return [HandoffCompactionStrategy(model_client=summary_client, model="mock-model")]


async def main() -> None:
    """两轮 warmup 累积 history，第 3 轮首采样 overflow → 自愈重采样成功。"""
    import tempfile

    # call 序列：t1=1, t2=2, t3 首采样=3(overflow) → 自愈重采样=4(ok)
    client = _OverflowOnNthClient(fail_on=3)
    with tempfile.TemporaryDirectory() as td:
        pool = await taifeng.EnginePool.create(
            skills_dir=SKILLS_DIR,
            threads_dir=Path(td) / "threads",
            model_client=client,
            # 大 window：warmup 轮不触发本地主动压缩，把舞台留给 provider overflow 自愈
            budget=ContextBudget(context_window=200_000, preserve_tail_messages=2),
            compressors=_summary_strategies(),
        )
        engine = await pool.get_or_create(
            session_id="demo-overflow", entry_skill_id="chatty-assistant"
        )
        sink_task = attach_console_sink(engine, color=True)

        events: list = []
        forward = asyncio.create_task(_collect(engine, events))

        for i in range(3):
            sub_id = await engine.submit(
                taifeng.UserMessage(text=f"第{i + 1}轮：请简要回应。")
            )
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    break

        await asyncio.sleep(0.5)
        await pool.close()
        await asyncio.sleep(0.2)
        sink_task.cancel()
        forward.cancel()

        # ── 结论 ──
        retried = sum(1 for m in events if m.kind == "provider_retry")
        ov = [
            m for m in events
            if m.kind == "compaction_started" and m.data.get("phase") == "overflow"
        ]
        finals = [m.kind for m in events if m.kind in ("turn_completed", "turn_failed")]
        print("\n" + "=" * 60)
        print(f"provider_retry 自愈触发 = {retried}")
        print(f"compaction phase=overflow = {len(ov)}")
        print(f"turn 结局序列 = {finals}  采样总次数 = {client.calls}")
        ok = retried > 0 and len(ov) > 0 and finals.count("turn_failed") == 0
        print(
            f"==> overflow 有界自愈{'确证 ✅' if ok else '未触发 ❓'}"
            "（provider 判超长 → 强制压缩 + 重采样，未硬失败丢 turn）"
        )
        print("=" * 60)


async def _collect(engine: object, events: list) -> None:
    """旁路订阅全事件用于收尾统计。"""
    async for ev in engine.subscribe_all():  # type: ignore[attr-defined]
        events.append(ev.msg)


if __name__ == "__main__":
    asyncio.run(main())
