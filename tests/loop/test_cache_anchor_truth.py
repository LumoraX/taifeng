"""Wave 2c 复现:cache anchor 真值化 + 坏 JSON 参数处置。

对应 openspec change ``wave2c-cache-anchor-truth``。每条用例先于修复写出,
改动前必须 FAIL(失败原因见 change tasks 1.1):
  a) 采样成功后 anchor 从不推进 → 第二次请求无 cache_breakpoints、runner anchor 恒 -1
  b) mid-turn 压缩上下文拿到的 anchor 恒 -1(「只动 tail」保护的是空集)
  c) sliding 窗口起点 = anchor(anchor 本条被压掉)
  d) handoff 窗口起点 = anchor
  e) surgical 常规窗口起点 = anchor;越界判定 ``i < anchor`` 漏掉 anchor 本条
  f) prompt 映射:anchor=0 不打点、来源判定 ``< anchor``(不含语义)
  g) overflow 自愈只有 DO_NOT_INJECT 一档:anchor 后 tail 过窄即放弃压缩
  h) 坏 JSON / 非对象参数退化为 {} 照常执行 handler
  i) seed retry / resumed tool 两条路径同样退化为 {}
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies import HandoffCompactionStrategy
from taifeng.context.strategies.sliding import SlidingWindowStrategy
from taifeng.context.strategies.surgical_trim import SurgicalTrimStrategy
from taifeng.conversation.models import (
    assistant_message,
    function_call,
    function_call_output,
    suspension_item,
    user_message,
)
from taifeng.llm.errors import ContextOverflowError
from taifeng.llm.events import completed, created, server_model, text_delta
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.prompt import build_api_request
from taifeng.loop.turn import TurnRunner
from taifeng.skill.definition import SkillDefinition
from taifeng.skill.dispatch import DispatchPolicy
from taifeng.skill.registry import SkillSnapshot
from taifeng.tool.registry import ToolRegistry
from taifeng.tool.runtime import ToolCallRuntime
from taifeng.tool.spec import ToolResult, ToolSpec
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.llm.types import ApiRequest

TID = "t-anchor"
DNI = InitialContextInjection.DO_NOT_INJECT
BLUM = InitialContextInjection.BEFORE_LAST_USER_MESSAGE


# ───────────────────────── 装配 ─────────────────────────


def _entry(tool_names: frozenset[str] = frozenset()) -> SkillDefinition:
    """entry-eligible composite skill(装配测试用,不实际 IO)。"""
    from pathlib import Path as _P

    return SkillDefinition(
        id="e", name="e", description="测试入口", version="1.0.0",
        type="composite", entry=True, body="入口", body_path=_P("_test_e.md"),
        child_skills=frozenset(), tool_names=tool_names, max_call_depth=3,
    )


def _echo_tool(calls: list[dict[str, Any]]) -> ToolSpec:
    """记录每次被调用的参数;handler 是否被执行是 D4 的核心断言点。"""

    async def _handler(args: dict[str, Any], ctx: object) -> ToolResult:
        calls.append(dict(args))
        return ToolResult.ok("ok")

    return ToolSpec(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {}},
        handler=_handler, parallel_safe=True,
    )


class _FakeStore:
    """最小内存 store。"""

    def __init__(self) -> None:
        self.items: list[object] = []

    async def append(self, item: object) -> None:
        self.items.append(item)

    async def create_thread(self, **_: object) -> str:
        return "sub-thread"


def _runner(
    *,
    client: Any,
    tools: list[ToolSpec] | None = None,
    history: list[Any],
    compressors: CompressionOrchestrator | None = None,
    budget: ContextBudget | None = None,
    events: list[Any] | None = None,
    **extra: Any,
) -> TurnRunner:
    entry = _entry(frozenset(t.name for t in (tools or [])))

    async def _emit(ev: Any) -> None:
        if events is not None:
            events.append(ev.msg)

    return TurnRunner(
        entry_skill=entry,
        snapshot=SkillSnapshot(version=1, skills=(entry,)),
        model_client=client,
        tool_runtime=ToolCallRuntime(ToolRegistry(tools or [])),
        store=_FakeStore(),
        compressors=compressors,
        dispatch_policy=DispatchPolicy(),
        budget=budget or ContextBudget(),
        thread_id=TID,
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
        history_buffer=history,
        **extra,
    )


def _pair(call_id: str, output: str) -> list[Any]:
    return [
        function_call(call_id=call_id, name="read_file", arguments="{}", thread_id=TID),
        function_call_output(call_id=call_id, output=output, thread_id=TID),
    ]


def _ctx(
    history: list[Any], *, anchor: int, phase: str = "mid_turn",
    token_estimate: int = 4_000, budget: ContextBudget | None = None,
) -> CompressionContext:
    return CompressionContext(
        history=history, token_estimate=token_estimate,
        budget=budget or ContextBudget(context_window=10_000),
        cache_anchor_index=anchor, phase=phase,  # type: ignore[arg-type]
        available_injections=frozenset(InitialContextInjection),
    )


class _ProbeStrategy:
    """恒触发、不改 history,只记录压缩上下文里的 (phase, anchor, len)。"""

    name = "probe"
    priority = 100

    def __init__(self) -> None:
        self.seen: list[tuple[str, int, int]] = []

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger:
        return CompressionTrigger(reason="token_limit", threshold_pct=1.0)

    async def compress(
        self, ctx: CompressionContext, injection: InitialContextInjection
    ) -> CompressionResult:
        self.seen.append((ctx.phase, ctx.cache_anchor_index, len(ctx.history)))
        return CompressionResult(
            success=False, cache_invalidated=False,
            anchor_preserved_until=ctx.cache_anchor_index, reason="probe",
        )


class _OverflowOnceSession:
    """首次 stream 抛 ContextOverflowError,之后正常返回。"""

    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    async def __aenter__(self) -> _OverflowOnceSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def stream(self, request: ApiRequest) -> AsyncIterator[Any]:
        if self._fail:
            raise ContextOverflowError("sim: context too long")
        yield created()
        yield server_model("mock-model")
        yield text_delta("recovered")
        yield completed(
            response_id="r", usage=TokenUsage(input_tokens=10, output_tokens=5),
            end_turn=True,
        )


class _OverflowOnceClient:
    """第一次采样 overflow,第二次成功;记 session 次数。"""

    def __init__(self) -> None:
        self.calls = 0

    def session(self, *, cancel: CancellationToken, model: str | None = None) -> Any:
        self.calls += 1
        return _OverflowOnceSession(fail=self.calls == 1)


def _summary_compressor() -> CompressionOrchestrator:
    summary_client = SimClient(turns=[SimTurn(
        text="## 进度\n摘要", usage=TokenUsage(input_tokens=400, output_tokens=20),
    )])
    return CompressionOrchestrator(
        [HandoffCompactionStrategy(model_client=summary_client, model="mock-model")]
    )


# ───────────────────────── a / b:anchor 推进 ─────────────────────────


async def test_anchor_advances_after_successful_sample(sim_client: Any) -> None:
    """a) 首采样发出 [user] 成功 → anchor=0;第二次请求打点在 user 消息;turn 结束
    anchor = 第二次发出长度([user, assistant, fc, fco]) - 1 = 3。"""
    calls: list[dict[str, Any]] = []
    client = sim_client(turns=[
        SimTurn(tool_calls=[{"id": "c1", "name": "echo", "arguments": "{}"}]),
        SimTurn(text="done"),
    ])
    runner = _runner(
        client=client, tools=[_echo_tool(calls)],
        history=[user_message("hi", thread_id=TID)],
    )
    outcome = await runner.run()
    assert outcome.success
    reqs = client.ledger.requests()
    assert len(reqs) == 2
    assert [bp.index for bp in reqs[0].request.cache_breakpoints] == []
    assert [bp.index for bp in reqs[1].request.cache_breakpoints] == [0], (
        "第二次请求应在上次发出的末条消息(user@m0)打 cache 点"
    )
    assert runner.cache_anchor_index == 3


async def test_mid_turn_compaction_sees_advanced_anchor(sim_client: Any) -> None:
    """b) 工具回填后的 mid_turn 压缩,ctx.anchor 必须等于首采样发出长度 - 1
    (= 当时 history 长度 - [assistant, fc, fco] 三条 - 1),而不是 -1。"""
    calls: list[dict[str, Any]] = []
    probe = _ProbeStrategy()
    client = sim_client(turns=[
        SimTurn(tool_calls=[{"id": "c1", "name": "echo", "arguments": "{}"}]),
        SimTurn(text="done"),
    ])
    runner = _runner(
        client=client, tools=[_echo_tool(calls)],
        history=[user_message("hi", thread_id=TID)],
        compressors=CompressionOrchestrator([probe]),
        budget=ContextBudget(context_window=10),  # soft 必超 → mid_turn 必触发
    )
    outcome = await runner.run()
    assert outcome.success
    mid = [(anchor, n) for phase, anchor, n in probe.seen if phase == "mid_turn"]
    assert len(mid) == 1, probe.seen
    anchor, n = mid[0]
    assert anchor == n - 3 - 1, f"mid_turn anchor 应为首采样发出长度-1,实得 {anchor}(len={n})"


# ───────────────────────── c / d / e:策略窗口 anchor+1 ─────────────────────────


async def test_sliding_window_starts_after_anchor() -> None:
    """c) anchor=2 → history[0..2] 逐项保留,compacted 占位从下标 3 起。"""
    items = [user_message(f"m{i} " + "x" * 100, thread_id=TID) for i in range(10)]
    budget = ContextBudget(context_window=1000, preserve_tail_messages=2)
    ctx = _ctx(items, anchor=2, token_estimate=int(budget.hard_limit) + 1, budget=budget)
    res = await SlidingWindowStrategy(keep_tail=2).compress(ctx, DNI)
    assert res.success and res.cache_invalidated is False
    assert res.new_history[:3] == items[:3], "anchor 本条(下标 2)不可动"
    assert res.new_history[3].kind == "compacted"
    assert res.anchor_preserved_until == 2


async def test_handoff_window_starts_after_anchor() -> None:
    """d) handoff 同规则:anchor=2 → 下标 0..2 原样,summary 从下标 3 起。"""
    client = SimClient(turns=[SimTurn(
        text="## summary", usage=TokenUsage(input_tokens=100, output_tokens=10),
    )])
    strategy = HandoffCompactionStrategy(model_client=client, model="mock-model")
    budget = ContextBudget(context_window=1000, preserve_tail_messages=2)
    items = [user_message(f"m{i} " + "x" * 200, thread_id=TID) for i in range(10)]
    ctx = _ctx(items, anchor=2, token_estimate=int(budget.soft_limit) + 100, budget=budget)
    res = await strategy.compress(ctx, DNI)
    assert res.success and res.cache_invalidated is False
    assert res.new_history[:3] == items[:3], "anchor 本条(下标 2)不可动"
    assert res.new_history[3].kind == "compacted"
    assert res.anchor_preserved_until == 2


async def test_surgical_normal_window_starts_after_anchor() -> None:
    """e1) anchor 恰指向一条超阈值 output(下标 2)→ 它已缓存,只剪 anchor 之后那条。"""
    hist = (
        [user_message("u", thread_id=TID)]
        + _pair("c1", "B" * 5_000)      # 下标 1, 2 —— 2 是 anchor 本条
        + _pair("c2", "C" * 5_000)      # 下标 3, 4
        + [assistant_message("tail", thread_id=TID, model="m")]
    )
    s = SurgicalTrimStrategy(protect_tail_messages=1, min_dedup_chars=10**6)
    res = await s.compress(_ctx(hist, anchor=2), DNI)
    assert res.success and res.cache_invalidated is False
    assert res.detail["soft_trimmed"] == 1, res.detail
    assert res.new_history[2].payload == hist[2].payload, "anchor 本条被改写"
    assert res.anchor_preserved_until == 2


async def test_surgical_hard_clear_on_anchor_item_marks_invalidated() -> None:
    """e2) allow_head_clear 下 hard-clear 改写了下标恰等于 anchor 的 output →
    必须如实标 cache_invalidated=True 并回退 anchor。"""
    hist = (
        [user_message("u", thread_id=TID)]
        + _pair("c1", "E" * 5_000)      # 下标 1, 2 —— 2 是 anchor 本条
        + [assistant_message(f"tail{i}", thread_id=TID, model="m") for i in range(2)]
    )
    s = SurgicalTrimStrategy(protect_tail_messages=2, allow_head_clear=True)
    res = await s.compress(
        _ctx(hist, anchor=2, phase="pre_turn", token_estimate=6_000), BLUM
    )
    assert res.success and res.detail["hard_cleared"] == 1
    assert res.cache_invalidated is True
    assert res.anchor_preserved_until == 1


# ───────────────────────── f:prompt 映射含语义 ─────────────────────────


def _req(history: list[Any], anchor: int) -> Any:
    return build_api_request(
        entry=_entry(), snapshot=SkillSnapshot(version=1, skills=()),
        history=history, tools=[], model="m", cache_anchor_index=anchor,
    )


def test_prompt_breakpoint_inclusive_anchor() -> None:
    """f) anchor=0 且 history[0] 产出消息 → 打点 [0];记账项偏移下取 <= anchor。"""
    assert [bp.index for bp in _req([user_message("a", thread_id=TID)], 0).cache_breakpoints] == [0]
    hist = [
        user_message("a", thread_id=TID),                                    # h0 → m0
        suspension_item(record_id="r", submission_id="s", turn_index=1,
                        pending=[], created_at=0, thread_id=TID),             # h1 → 无产出
        assistant_message("b", thread_id=TID, model="m"),                     # h2 → m1
    ]
    assert [bp.index for bp in _req(hist, 2).cache_breakpoints] == [1]
    assert _req(hist, -1).cache_breakpoints == []


# ───────────────────────── g:overflow 两档 ─────────────────────────


async def test_overflow_second_stage_compacts_head_when_tail_too_narrow() -> None:
    """g) anchor=7、10 条 history、保尾 4:第一档 [8, 6) 过窄;第二档必须动 head 成功
    压缩,break 标 expected(reason=compaction_overflow),重采样恰一次。"""
    events: list[Any] = []
    history = [user_message(f"m{i} " + "x" * 120, thread_id=TID) for i in range(10)]
    client = _OverflowOnceClient()
    runner = _runner(
        client=client, history=history, compressors=_summary_compressor(),
        events=events, cache_anchor_index=7,
    )
    outcome = await runner.run()
    assert outcome.success and outcome.end_reason == "completed"
    assert client.calls == 2
    assert len([m for m in events if m.kind == "provider_retry"]) == 1
    done = [m for m in events if m.kind == "compaction_completed"]
    assert any(m.data["success"] and m.data["cache_invalidated"] for m in done), (
        f"第二档应动 head 成功压缩,实得 {[m.data for m in done]}"
    )
    assert len(runner.history_buffer) < len(history)
    assert runner._next_cache_break_expected is True  # noqa: SLF001
    assert runner._next_cache_break_reason == "compaction_overflow"  # noqa: SLF001


# ───────────────────────── h / i:坏参数 → invalid_arguments ─────────────────────────


async def test_bad_json_arguments_rejected_not_executed(sim_client: Any) -> None:
    """h) 坏 JSON 与非对象 JSON 均不执行 handler,以 invalid_arguments error fco 核销,
    模型在下一次请求里看得到错误正文。"""
    calls: list[dict[str, Any]] = []
    events: list[Any] = []
    client = sim_client(turns=[
        SimTurn(tool_calls=[{"id": "c1", "name": "echo", "arguments": "{not json"}]),
        SimTurn(tool_calls=[{"id": "c2", "name": "echo", "arguments": "[1, 2]"}]),
        SimTurn(text="done"),
    ])
    runner = _runner(
        client=client, tools=[_echo_tool(calls)],
        history=[user_message("hi", thread_id=TID)], events=events,
    )
    outcome = await runner.run()
    assert outcome.success
    assert calls == [], f"坏参数不得执行 handler,实得 {calls}"
    fcos = [it for it in runner.history_buffer if it.kind == "function_call_output"]
    assert [it.payload["call_id"] for it in fcos] == ["c1", "c2"]
    for it in fcos:
        assert it.payload["is_error"] is True
        assert it.payload["output"].startswith("invalid_arguments:"), it.payload["output"]
    completed_evs = [m for m in events if m.kind == "tool_call_completed"]
    assert len(completed_evs) == 2 and all(m.data["is_error"] for m in completed_evs)
    assert client.ledger.function_call_output_text("c1").startswith("invalid_arguments:")
    assert client.ledger.function_call_output_text("c2").startswith("invalid_arguments:")


async def test_seed_retry_bad_json_rejected(sim_client: Any) -> None:
    """i1) rewind retry_tool 补跑悬空 fc:参数坏 JSON → 不执行,error fco 核销。"""
    calls: list[dict[str, Any]] = []
    client = sim_client(turns=[SimTurn(text="done")])
    runner = _runner(
        client=client, tools=[_echo_tool(calls)],
        history=[
            user_message("hi", thread_id=TID),
            function_call(call_id="c1", name="echo", arguments="{bad", thread_id=TID),
        ],
    )
    await runner._complete_seed_call("c1")  # noqa: SLF001
    assert calls == []
    tail = runner.history_buffer[-1]
    assert tail.kind == "function_call_output" and tail.payload["call_id"] == "c1"
    assert tail.payload["is_error"] is True
    assert tail.payload["output"].startswith("invalid_arguments:")


async def test_resumed_tool_bad_json_rejected(
    skills_dir: Path, threads_dir: Path, sim_client: Any,
) -> None:
    """i2) engine resumed tool:history 里的 fc 参数坏 JSON → 不执行,error fco 结算。"""
    calls: list[dict[str, Any]] = []
    client = sim_client(turns=[])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[_echo_tool(calls)],
    )
    try:
        engine = await pool.get_or_create(
            session_id="s_bad_args", entry_skill_id="code-reviewer",
        )
        # resumed tool 的 token 派生自根 token(R4):等 engine 主循环把它备好
        await wait_for_condition(
            lambda: engine._root_cancel is not None,  # noqa: SLF001
            deadline_seconds=5.0, message="engine 未就绪",
        )
        engine._history.append(function_call(  # noqa: SLF001
            call_id="c1", name="echo", arguments="{bad", thread_id=engine.thread_id,
        ))
        await engine._execute_resumed_tool("c1")  # noqa: SLF001
        assert calls == []
        tail = engine._history[-1]  # noqa: SLF001
        assert tail.kind == "function_call_output" and tail.payload["call_id"] == "c1"
        assert tail.payload["is_error"] is True
        assert tail.payload["output"].startswith("invalid_arguments:")
    finally:
        await pool.close()
