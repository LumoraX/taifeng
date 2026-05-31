"""K3：长期记忆 swap/缺页接口 —— prefetch / writeback / on_pre_evict / on_session_end。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.conversation.models import (
    ResponseItem,
    compacted,
    user_message,
)
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.turn import TurnRunner
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime


class _RecordingMemory:
    """记录各钩子调用的假 MemoryStore。"""

    def __init__(self, prefetch_text: str = "", evict_digest: str = "") -> None:
        self.prefetch_calls: list[tuple[str, str]] = []
        self.writeback_items: list[ResponseItem] = []
        self.evicted: list[ResponseItem] = []
        self.session_end = False
        self._pf = prefetch_text
        self._ev = evict_digest

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        self.prefetch_calls.append((query, thread_id))
        return self._pf

    async def writeback(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        self.writeback_items.extend(items)

    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        self.evicted.extend(items)
        return self._ev

    async def on_session_end(
        self, *, thread_id: str, items: Sequence[ResponseItem]
    ) -> None:
        self.session_end = True


class _FakeStore:
    async def create_thread(self, **_: object) -> str:
        return "t"

    async def append(self, item: object) -> None:
        return None


async def _runner(skills_dir: Path, **extra: object) -> TurnRunner:
    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    assert entry is not None

    async def _emit(ev: object) -> None:
        return None

    return TurnRunner(
        entry_skill=entry, snapshot=reg.snapshot(),
        model_client=MockClient(turns=[MockTurn(text="ok")]),
        tool_runtime=ToolCallRuntime(ToolRegistry()), store=_FakeStore(),
        compressors=None, dispatch_policy=DispatchPolicy(), budget=ContextBudget(),
        thread_id="t", submission_id="s", emit=_emit,
        cancel=CancellationToken(name="t"),
        history_buffer=[user_message("hello world", thread_id="t")],
        **extra,
    )


@pytest.mark.asyncio
async def test_build_api_request_injects_prefetched_memory_in_tail(
    skills_dir: Path,
) -> None:
    """prefetched_memory → 尾部 system 消息（不动 system_prompt 头部，cache-aware）。"""
    from taifeng.loop.prompt import build_api_request

    r = await FilesystemSkillRegistry.load(skills_dir)
    entry = r.get("code-reviewer")
    assert entry is not None
    req = build_api_request(
        entry=entry, snapshot=r.snapshot(),
        history=[user_message("hi", thread_id="t")], tools=[], model="m",
        prefetched_memory="REMEMBERED_FACT",
    )
    assert "REMEMBERED_FACT" not in req.system_prompt[0]  # 头部不含
    sys_msgs = [m for m in req.messages if m.role == "system"]
    assert any("REMEMBERED_FACT" in m.content for m in sys_msgs)  # 尾部含


@pytest.mark.asyncio
async def test_prefetch_called_with_last_user_query_and_writeback(
    skills_dir: Path,
) -> None:
    mem = _RecordingMemory(prefetch_text="CTX")
    runner = await _runner(skills_dir, memory_store=mem)
    await runner.run()
    # page-in：用最近 user 消息作 query
    assert mem.prefetch_calls == [("hello world", "t")]
    assert runner._prefetched_memory == "CTX"  # noqa: SLF001
    # dirty-page writeback：本 turn 新增（assistant）被写回
    assert any(it.kind == "assistant_message" for it in mem.writeback_items)


@pytest.mark.asyncio
async def test_pre_evict_salvages_and_injects_digest(skills_dir: Path) -> None:
    mem = _RecordingMemory(evict_digest="KEEP: id=42")
    runner = await _runner(skills_dir, memory_store=mem)
    u1 = user_message("old1", thread_id="t")
    u2 = user_message("old2", thread_id="t")
    tail = user_message("recent", thread_id="t")
    summary = compacted("summary", thread_id="t", replaced_range=(0, 2),
                        cache_invalidated=False)
    before = [u1, u2, tail]
    after = [summary, tail]  # u1,u2 换出
    out = await runner._apply_pre_evict_salvage(  # noqa: SLF001
        before, after, summary.id
    )
    # 换出的 items 交给 memory
    assert mem.evicted == [u1, u2]
    # digest 作为 system_injection 插在 summary 之后
    assert out[1].kind == "system_injection"
    assert "KEEP: id=42" in out[1].payload["text"]


@pytest.mark.asyncio
async def test_no_memory_store_is_noop(skills_dir: Path) -> None:
    """memory_store=None（默认）→ 行为不变（不注入、不调钩子）。"""
    runner = await _runner(skills_dir)  # 无 memory_store
    await runner.run()
    assert runner._prefetched_memory == ""  # noqa: SLF001


@pytest.mark.asyncio
async def test_engine_on_session_end_on_shutdown(
    skills_dir: Path, threads_dir: Path
) -> None:
    mem = _RecordingMemory()
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=MockClient(turns=[MockTurn(text="ok")]),
        compressors=[], memory_store=mem,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="code-reviewer")
    sub = await engine.submit(taifeng.UserMessage(text="hi"))
    async for ev in engine.subscribe(sub):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            break
    await pool.close()  # 发 Shutdown → on_session_end
    assert mem.session_end is True
    assert mem.prefetch_calls  # turn 内 page-in 也发生过
