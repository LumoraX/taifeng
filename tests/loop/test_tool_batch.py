"""tool_batch.dispatch_batch —— 批处理器单测(注入 fake runtime/hook)。"""
from __future__ import annotations

import asyncio

from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.tool_batch import ToolCallRequest, dispatch_batch
from taifeng.tool.spec import ToolContext, ToolResult


class _FakeRuntime:
    """记录 dispatch 调用顺序,按 name 返回 ToolResult。可选 delay 模拟耗时。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self._delay = delay

    async def dispatch(self, *, name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        self.calls.append(name)
        return ToolResult.ok(f"out:{name}")


def _ctx(call_id: str) -> ToolContext:
    return ToolContext(call_id=call_id, cancel=CancellationToken(), thread_id="t1", extras={})


def _reqs(n: int) -> list[ToolCallRequest]:
    return [
        ToolCallRequest(
            index=i, call_id=f"c{i}", name=chr(ord("a") + i),
            arguments={}, arguments_raw="{}", parallel_safe=True,
        )
        for i in range(n)
    ]


async def _run(reqs: list[ToolCallRequest], runtime: _FakeRuntime, cap: int):
    events: list[tuple[str, str]] = []

    async def emit(msg) -> None:
        events.append((msg.kind, msg.data.get("call_id", "")))

    outcomes = await dispatch_batch(
        reqs,
        runtime=runtime,
        ctx_for=_ctx,
        hooks=None,
        emit=emit,
        semaphore=asyncio.Semaphore(cap),
        thread_id="t1",
        submission_id="s1",
        entry_skill_id="e1",
        # 测试 fake 工具名 a..z 全部视为本轮已提供（白名单校验另有专测）
        visible_tools=frozenset({chr(c) for c in range(ord("a"), ord("z") + 1)}),
    )
    return outcomes, events


class _ArgsRecordingRuntime:
    """记录每次 dispatch 收到的 arguments，用于验证 args_override。"""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def dispatch(
        self, *, name: str, arguments: dict, ctx: ToolContext
    ) -> ToolResult:
        self.seen.append(arguments)
        return ToolResult.ok(f"out:{name}")


async def test_g5a_pre_tool_use_hook_args_override() -> None:
    """PreToolUse hook 放行并给 args_override → dispatch 收到改写后的 args。"""
    from taifeng.hooks.types import HookDecision, HookRunner

    async def _rewrite(hook: object, ctx: object) -> HookDecision:
        # 把任意 args 改写为 {"path": "/safe"}
        return HookDecision.ok(args_override={"path": "/safe"})

    runner = HookRunner()
    runner.registry.register("pre_tool_use", _rewrite)

    runtime = _ArgsRecordingRuntime()
    req = ToolCallRequest(
        index=0, call_id="c0", name="file_read",
        arguments={"path": "/etc/passwd"}, arguments_raw="{}",
        parallel_safe=True,
    )

    async def emit(msg) -> None:  # noqa: ANN001
        return None

    outcomes = await dispatch_batch(
        [req], runtime=runtime, ctx_for=_ctx, hooks=runner, emit=emit,
        semaphore=asyncio.Semaphore(4),
        thread_id="t1", submission_id="s1", entry_skill_id="e1",
        visible_tools=frozenset({"file_read"}),
    )
    assert outcomes[0].result.output == "out:file_read"
    # dispatch 实际收到的是被 hook 改写后的 args
    assert runtime.seen == [{"path": "/safe"}]


async def test_dispatch_batch_preserves_index_order() -> None:
    """结果按 request.index 升序返回,与完成顺序无关。"""
    runtime = _FakeRuntime()
    outcomes, events = await _run(_reqs(2), runtime, cap=4)
    assert [o.index for o in outcomes] == [0, 1]
    assert [o.result.output for o in outcomes] == ["out:a", "out:b"]
    completed = [cid for k, cid in events if k == "tool_call_completed"]
    assert set(completed) == {"c0", "c1"}


async def test_dispatch_batch_runs_concurrently() -> None:
    """cap=4、每条 sleep 0.2s 的 3 条:wall-clock 应明显 < 0.6s(串行和)。"""
    runtime = _FakeRuntime(delay=0.2)
    start = asyncio.get_event_loop().time()
    outcomes, _ = await _run(_reqs(3), runtime, cap=4)
    elapsed = asyncio.get_event_loop().time() - start
    assert len(outcomes) == 3
    assert elapsed < 0.5, f"期望并发(<0.5s),实测 {elapsed:.2f}s"


async def test_dispatch_batch_serial_when_cap_one() -> None:
    """cap=1、每条 sleep 0.2s 的 3 条:wall-clock 应 >= 0.6s(串行)。"""
    runtime = _FakeRuntime(delay=0.2)
    start = asyncio.get_event_loop().time()
    await _run(_reqs(3), runtime, cap=1)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.55, f"期望串行(>=0.55s),实测 {elapsed:.2f}s"


class _CancelAwareRuntime:
    """dispatch 阻塞在 ctx.cancel.wait_cancelled();取消级联到达后返回 cancelled 错误。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def dispatch(self, *, name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
        self.started.set()
        await ctx.cancel.wait_cancelled()  # 等父 turn 取消级联到本分支
        return ToolResult.error("cancelled", reason="cancelled")


async def test_dispatch_batch_cancel_cascades_to_all_branches() -> None:
    """父 token 取消 → 所有并发分支的 cancel.child 同步取消 → 各自收敛,不挂死。"""
    parent = CancellationToken(name="turn")
    runtime = _CancelAwareRuntime()

    def ctx_for(call_id: str) -> ToolContext:
        # 每个分支拿父 token 的子 token（与 turn.py._build_tool_context 一致语义）
        return ToolContext(
            call_id=call_id, cancel=parent.child(f"tool:{call_id}"),
            thread_id="t1", extras={},
        )

    async def emit(msg) -> None:  # noqa: ANN001
        return None

    async def canceller() -> None:
        await runtime.started.wait()
        await asyncio.sleep(0.02)
        parent.cancel()  # 级联到两个分支的 child token

    outcomes, _ = await asyncio.gather(
        dispatch_batch(
            _reqs(2), runtime=runtime, ctx_for=ctx_for, hooks=None, emit=emit,
            semaphore=asyncio.Semaphore(2),
            thread_id="t1", submission_id="s1", entry_skill_id="e1",
            visible_tools=frozenset({"a", "b"}),
        ),
        canceller(),
    )
    # 两个分支都收敛为 cancelled 错误（不挂死）
    assert len(outcomes) == 2
    assert all(o.result.is_error for o in outcomes)
    assert all(o.result.data.get("reason") == "cancelled" for o in outcomes)
