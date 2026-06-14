"""审计可观测 层1 —— 事件总线序号契约。

覆盖：
- 全局单调 ``seq``（在 ``engine._emit`` 入口分配，原子不重不漏）；
- per-subscriber 投递序号 ``delivery_seq``（每订阅从 0 连续，丢弃烧号 → 跳号=该订阅自己漏了）；
- 高/低水位告警（迟滞，仅有界队列生效）；
- ``engine.session_id`` 只读属性（组 ``(session_id, seq)`` 落库键用）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import taifeng
from taifeng.llm.providers import SimClient
from taifeng.loop.event import EngineLog, EventMsg
from taifeng.skill.registry import FilesystemSkillRegistry
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime


class _FakeStore:
    async def create_thread(self, **_: object) -> str:
        return "t"

    async def append(self, item: object) -> None:
        return None


async def _make_engine(skills_dir: Path, **kw: object) -> taifeng.AgentEngine:
    reg = await FilesystemSkillRegistry.load(skills_dir)
    entry = reg.get("code-reviewer")
    assert entry is not None
    return taifeng.AgentEngine(
        entry_skill=entry,
        skill_snapshot=reg.snapshot(),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        model_client=SimClient(turns=[]),
        store=_FakeStore(),
        thread_id="t",
        **kw,
    )


def _ev() -> EventMsg:
    return EventMsg(submission_id="s", msg=EngineLog(data={"level": "info"}))


@pytest.mark.asyncio
async def test_emit_assigns_monotonic_global_seq(skills_dir: Path) -> None:
    """全局 seq 在 _emit 入口同步分配，从 0 起单调递增、不重。"""
    engine = await _make_engine(skills_dir)
    e0, e1, e2 = _ev(), _ev(), _ev()
    await engine._emit(e0)  # noqa: SLF001
    await engine._emit(e1)  # noqa: SLF001
    await engine._emit(e2)  # noqa: SLF001
    assert (e0.seq, e1.seq, e2.seq) == (0, 1, 2)


@pytest.mark.asyncio
async def test_per_subscriber_delivery_seq_contiguous_and_gap_on_drop(
    skills_dir: Path,
) -> None:
    """每订阅 delivery_seq 从 0 连续；队列满丢弃也'烧号'→ 收方跳号=自己漏了。"""
    from taifeng.loop.engine import _Subscriber

    engine = await _make_engine(skills_dir, event_queue_size=2)
    sub = _Subscriber(maxsize=2, high_ratio=0.75, low_ratio=0.5)
    engine._all_subs.append(sub)  # noqa: SLF001

    # 队列容量 2、不消费；emit 3 条 → 第 3 条丢弃
    for _ in range(3):
        await engine._emit(_ev())  # noqa: SLF001

    delivered = [sub.queue.get_nowait(), sub.queue.get_nowait()]
    assert [d.delivery_seq for d in delivered] == [0, 1]
    # 第 3 条烧掉了 delivery_seq=2 → 下一次投递序号已到 3，消费者据此知道自己漏了 1 条
    assert sub.next_delivery == 3
    assert engine.events_dropped == 1


@pytest.mark.asyncio
async def test_high_water_warning_with_hysteresis(
    skills_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """有界队列 qsize 上穿高水位 → 一条 warning；迟滞下不重复刷。"""
    from taifeng.loop.engine import _Subscriber

    engine = await _make_engine(skills_dir, event_queue_size=4)
    sub = _Subscriber(maxsize=4, high_ratio=0.75, low_ratio=0.5)  # high=3 low=2
    engine._all_subs.append(sub)  # noqa: SLF001

    with caplog.at_level("WARNING"):
        for _ in range(4):  # qsize 1→2→3(穿高水位)→4
            await engine._emit(_ev())  # noqa: SLF001

    hw = [r for r in caplog.records if "high-water" in r.getMessage()]
    assert len(hw) == 1  # 迟滞：穿过一次只告一条，不每条都刷


@pytest.mark.asyncio
async def test_no_high_water_warning_when_unbounded(
    skills_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """无界队列（size<=0）无容量百分比 → 不做高水位告警（opt-in 自负内存）。"""
    from taifeng.loop.engine import _Subscriber

    engine = await _make_engine(skills_dir, event_queue_size=0)
    sub = _Subscriber(maxsize=0, high_ratio=0.75, low_ratio=0.5)
    engine._all_subs.append(sub)  # noqa: SLF001

    with caplog.at_level("WARNING"):
        for _ in range(50):
            await engine._emit(_ev())  # noqa: SLF001

    assert not [r for r in caplog.records if "high-water" in r.getMessage()]


@pytest.mark.asyncio
async def test_session_id_property_explicit(skills_dir: Path) -> None:
    """显式传入 session_id → 只读属性如实暴露（供 sink 组落库键）。"""
    engine = await _make_engine(skills_dir, session_id="sess-1")
    assert engine.session_id == "sess-1"


@pytest.mark.asyncio
async def test_session_id_property_defaults_to_thread_id(skills_dir: Path) -> None:
    """未传 session_id → 退回 thread_id（保持单 engine 单 session 一对一）。"""
    engine = await _make_engine(skills_dir)
    assert engine.session_id == "t"
