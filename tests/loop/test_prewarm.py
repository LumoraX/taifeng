"""预热（prewarm，ADR 0092）：在用户输入到来之前做掉首轮采样的准备工作。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.instructions import InstructionLayer
from taifeng.llm.prewarm import CachePrimingPrewarmer, ModelPrewarmer, PrewarmOutcome
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.types import ApiMessage, ApiRequest, TokenUsage
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import CancelTurn, Prewarm
from taifeng.skill import DispatchPolicy
from taifeng.skill.fitness import InMemorySkillFitnessStore
from taifeng.skill.outcome import SkillExecutionRecord
from taifeng.skill.working_set import WorkingSetPolicy
from taifeng.skill.working_set_runtime import SkillWorkingSet
from tests.conftest import GUARD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from pathlib import Path

_USAGE = TokenUsage(input_tokens=40, output_tokens=1, total_tokens=41)

_ENTRY = """---
name: entry
description: 顶层入口
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [leaf]
tool_names: []
max_call_depth: 3
---
# ENTRY_BODY
"""

_LEAF = """---
name: leaf
description: 叶子
version: 1.0.0
type: atomic
---
# leaf
"""


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    for name, body in (("entry", _ENTRY), ("leaf", _LEAF)):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(body, encoding="utf-8")
    return root


class _Recorder:
    """记录收到的请求的预热器；可配置为失败、阻塞或不消耗。"""

    def __init__(
        self,
        *,
        usage: TokenUsage | None = _USAGE,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
        primed: bool = True,
    ) -> None:
        self.requests: list[ApiRequest] = []
        self.entered = asyncio.Event()
        self._usage = usage
        self._error = error
        self._gate = gate
        self._primed = primed

    async def prewarm(
        self, request: ApiRequest, *, cancel: CancellationToken,
    ) -> PrewarmOutcome:
        self.requests.append(request)
        self.entered.set()
        if self._gate is not None:
            while not self._gate.is_set():
                cancel.raise_if_cancelled()
                await asyncio.sleep(0.005)
        if self._error is not None:
            raise self._error
        return PrewarmOutcome(primed=self._primed, usage=self._usage)


class _Run:
    """一次测试用到的 pool / engine / client。"""

    def __init__(self) -> None:
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.client: SimClient

    async def start(self, tmp_path: Path, turns: list[SimTurn], **kwargs: Any) -> None:
        self.client = SimClient(turns=turns)
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), threads_dir=tmp_path / "threads",
            model_client=self.client, compressors=[], **kwargs,
        )
        self.engine = await self.pool.get_or_create(session_id="s", entry_skill_id="entry")

    async def submit(self, op: Any, *, until: tuple[str, ...]) -> list[taifeng.EventMsg]:
        """提交并收集该 submission 的事件，直到出现 ``until`` 里的某个 kind。"""
        holder: list[str] = []
        events: list[taifeng.EventMsg] = []

        async def collect() -> None:
            async for event in self.engine.subscribe_all():
                if not holder or event.submission_id != holder[0]:
                    continue
                events.append(event)
                if event.msg.kind in until:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        holder.append(await self.engine.submit(op))
        await asyncio.wait_for(task, timeout=GUARD_TIMEOUT_SECONDS)
        return events

    async def prewarm(self, **kwargs: Any) -> dict[str, Any]:
        events = await self.submit(Prewarm(**kwargs), until=("prewarm_completed",))
        assert [event.msg.kind for event in events][0] == "prewarm_started"
        return dict(events[-1].msg.data)

    async def ask(self, text: str = "你好") -> list[taifeng.EventMsg]:
        return await self.submit(
            taifeng.UserMessage(text=text), until=("turn_completed", "turn_failed"),
        )


def _say(text: str) -> SimTurn:
    return SimTurn(text=text, usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15))


# ====================================================================
# 模型侧预热
# ====================================================================


async def test_prewarm_sends_the_request_the_first_turn_will_send(tmp_path: Path) -> None:
    prewarmer = _Recorder()
    run = _Run()
    await run.start(tmp_path, [_say("回答")], model_prewarmer=prewarmer)

    completed = await run.prewarm()

    assert completed["steps"] == {
        "instructions": "skipped", "working_set": "skipped", "model": "primed",
    }
    assert completed["errors"] == {}
    assert completed["cancelled"] is False
    assert completed["usage"]["total_tokens"] == 41
    (warmed,) = prewarmer.requests
    assert warmed.messages == []

    await run.ask()

    first = run.client.ledger.requests()[0].request
    # 前缀逐项相同：system prompt、工具清单、模型
    assert warmed.system_prompt == first.system_prompt
    assert [tool.name for tool in warmed.tools] == [tool.name for tool in first.tools]
    assert warmed.model == first.model
    await run.pool.close()


async def test_prewarm_leaves_no_trace(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [_say("回答")], model_prewarmer=_Recorder())
    before = run.engine.introspect()

    await run.prewarm()

    assert list(run.engine.history_snapshot()) == []
    stored = [item async for item in await run.pool.store.load_thread(run.engine.thread_id)]
    assert stored == []
    assert run.engine.rewind_nodes() == []
    # 没有发生真实采样
    assert run.client.ledger.requests() == []
    after = run.engine.introspect()
    assert after["turn_index"] == before["turn_index"]

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    assert [item.kind for item in run.engine.history_snapshot()] == [
        "user_message", "assistant_message",
    ]
    await run.pool.close()


async def test_prewarm_usage_counts_towards_the_session(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [_say("回答")], model_prewarmer=_Recorder())

    await run.prewarm()

    assert run.engine.introspect()["usage"]["total_tokens"] == 41
    await run.pool.close()


async def test_exhausted_session_budget_skips_the_model_step(tmp_path: Path) -> None:
    prewarmer = _Recorder()
    run = _Run()
    await run.start(
        tmp_path, [_say("回答")], model_prewarmer=prewarmer, max_session_tokens=41,
    )
    await run.prewarm()

    completed = await run.prewarm()

    assert completed["steps"]["model"] == "skipped"
    assert completed["errors"] == {"model": "session_token_limit"}
    assert len(prewarmer.requests) == 1
    await run.pool.close()


async def test_without_prewarmer_the_model_step_is_unsupported(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, [_say("回答")])

    completed = await run.prewarm()

    assert completed["steps"]["model"] == "unsupported"
    assert completed["usage"] is None
    await run.pool.close()


async def test_prewarmer_may_report_nothing_to_do(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path, [_say("回答")], model_prewarmer=_Recorder(primed=False, usage=None),
    )

    completed = await run.prewarm()

    assert completed["steps"]["model"] == "unsupported"
    assert completed["usage"] is None
    await run.pool.close()


async def test_failure_is_reported_and_does_not_affect_the_turn(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path, [_say("回答")],
        model_prewarmer=_Recorder(error=RuntimeError("provider unreachable")),
    )

    completed = await run.prewarm()

    assert completed["steps"]["model"] == "failed"
    assert completed["errors"] == {"model": "RuntimeError: provider unreachable"}
    assert completed["cancelled"] is False

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    await run.pool.close()


async def test_selected_steps_only(tmp_path: Path) -> None:
    prewarmer = _Recorder()
    run = _Run()
    await run.start(tmp_path, [_say("回答")], model_prewarmer=prewarmer)

    completed = await run.prewarm(steps=("instructions",))

    assert completed["steps"] == {"instructions": "skipped"}
    assert prewarmer.requests == []
    await run.pool.close()


def test_unknown_step_is_rejected() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        Prewarm(steps=("memory",))  # type: ignore[arg-type]


# ====================================================================
# 让路与取消
# ====================================================================


async def test_user_message_cancels_an_unfinished_prewarm(tmp_path: Path) -> None:
    gate = asyncio.Event()
    prewarmer = _Recorder(gate=gate)
    run = _Run()
    await run.start(tmp_path, [_say("回答")], model_prewarmer=prewarmer)
    seen: list[taifeng.EventMsg] = []

    async def watch() -> None:
        async for event in run.engine.subscribe_all():
            seen.append(event)

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await run.engine.submit(Prewarm())
    await asyncio.wait_for(prewarmer.entered.wait(), timeout=GUARD_TIMEOUT_SECONDS)

    events = await run.ask()

    assert events[-1].msg.kind == "turn_completed"
    completed = next(e.msg.data for e in seen if e.msg.kind == "prewarm_completed")
    assert completed["cancelled"] is True
    assert completed["steps"]["model"] == "cancelled"
    assert completed["errors"] == {}
    order = [e.msg.kind for e in seen if e.msg.kind in ("prewarm_completed", "turn_started")]
    assert order == ["prewarm_completed", "turn_started"]
    # gate 从未打开：turn 没有等预热跑完
    assert not gate.is_set()
    watcher.cancel()
    await run.pool.close()


async def test_cancel_turn_cancels_the_prewarm(tmp_path: Path) -> None:
    prewarmer = _Recorder(gate=asyncio.Event())
    run = _Run()
    await run.start(tmp_path, [_say("回答")], model_prewarmer=prewarmer)
    seen: list[taifeng.EventMsg] = []

    async def watch() -> None:
        async for event in run.engine.subscribe_all():
            seen.append(event)
            if event.msg.kind == "prewarm_completed":
                return

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    sub_id = await run.engine.submit(Prewarm())
    await asyncio.wait_for(prewarmer.entered.wait(), timeout=GUARD_TIMEOUT_SECONDS)

    await run.engine.submit(CancelTurn(submission_id=sub_id))
    await asyncio.wait_for(watcher, timeout=GUARD_TIMEOUT_SECONDS)

    assert seen[-1].msg.data["cancelled"] is True
    assert list(run.engine.history_snapshot()) == []
    await run.pool.close()


async def test_prewarm_waits_for_a_running_turn(tmp_path: Path) -> None:
    """turn 在跑时提交的预热排在它后面，看到的是 turn 结束后的 history。"""
    prewarmer = _Recorder()
    run = _Run()
    await run.start(tmp_path, [_say("第一轮回答"), _say("第二轮回答")], model_prewarmer=prewarmer)
    await run.ask("第一轮")

    await run.prewarm()

    (warmed,) = prewarmer.requests
    assert [message.role for message in warmed.messages] == ["user", "assistant"]

    await run.ask("第二轮")

    second = run.client.ledger.requests()[-1].request
    assert second.messages[: len(warmed.messages)] == warmed.messages
    await run.pool.close()


# ====================================================================
# 指令层
# ====================================================================


class _CountingSource:
    """记录被取了几次的动态指令源。"""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, ctx: Any) -> str:
        self.calls += 1
        return "动态指令正文"


async def test_prewarm_resolves_instructions_ahead_of_the_turn(tmp_path: Path) -> None:
    source = _CountingSource()
    prewarmer = _Recorder()
    run = _Run()
    await run.start(
        tmp_path, [_say("回答")], model_prewarmer=prewarmer,
        instruction_layers=[InstructionLayer(
            name="dyn", source=source, scope="session", cache_ttl_seconds=600,
        )],
    )

    events = await run.submit(Prewarm(), until=("prewarm_completed",))

    assert events[-1].msg.data["steps"]["instructions"] == "resolved"
    assert "instruction_fetched" in [event.msg.kind for event in events]
    assert source.calls == 1
    # 预热的请求带着解析好的指令
    assert "动态指令正文" in "\n".join(prewarmer.requests[0].system_prompt)

    turn = await run.ask()

    # 首轮解析命中缓存，没有再取一次
    assert source.calls == 1
    assert "instruction_cache_hit" in [event.msg.kind for event in turn]
    first = run.client.ledger.requests()[0].request
    assert first.system_prompt == prewarmer.requests[0].system_prompt
    await run.pool.close()


async def test_instruction_failure_does_not_block_later_steps(tmp_path: Path) -> None:
    class _Broken:
        async def fetch(self, ctx: Any) -> str:
            raise RuntimeError("config service down")

    prewarmer = _Recorder()
    run = _Run()
    await run.start(
        tmp_path, [_say("回答")], model_prewarmer=prewarmer,
        instruction_layers=[InstructionLayer(name="dyn", source=_Broken(), scope="session")],
    )

    completed = await run.prewarm()

    assert completed["steps"]["instructions"] == "failed"
    assert "instructions" in completed["errors"]
    assert completed["steps"]["model"] == "primed"
    await run.pool.close()


# ====================================================================
# 工作集
# ====================================================================


async def test_prewarm_restores_the_working_set(tmp_path: Path) -> None:
    store = InMemorySkillFitnessStore()
    await store.record(SkillExecutionRecord(
        skill_id="leaf", call_id="c1", parent_call_id=None, depth=1, source="user",
        trust_tier=None, selection_origin="whitelist", selection_confidence=None,
        outcome="success", outcome_signal_source="structural", end_reason="completed",
        error_detail=None, cost_tokens=10, cost_duration_ms=5, cost_iterations=1, ts_unix=100,
    ))
    working_set = SkillWorkingSet(
        store=store,
        policy=WorkingSetPolicy(budget=2, promote_min_score=0.2, promote_min_samples=1),
    )
    run = _Run()
    await run.start(
        tmp_path, [_say("回答")], dispatch_policy=DispatchPolicy(working_set=working_set),
    )

    events = await run.submit(Prewarm(steps=("working_set",)), until=("prewarm_completed",))

    assert events[-1].msg.data["steps"] == {"working_set": "restored"}
    promoted = [e.msg.data["skill_id"] for e in events if e.msg.kind == "skill_promoted"]
    assert promoted == ["leaf"]
    assert working_set.promoted == ("leaf",)
    await run.pool.close()


# ====================================================================
# 参考实现
# ====================================================================


async def test_cache_priming_probe_keeps_the_prefix(tmp_path: Path) -> None:
    client = SimClient(turns=[SimTurn(text="p", usage=_USAGE)])
    prewarmer = CachePrimingPrewarmer(client)
    request = ApiRequest(
        model="mock-model",
        system_prompt=["你是助手"],
        messages=[ApiMessage(role="user", content="之前的问题"),
                  ApiMessage(role="assistant", content="之前的回答")],
        max_output_tokens=2048,
    )

    outcome = await prewarmer.prewarm(request, cancel=CancellationToken())

    assert isinstance(prewarmer, ModelPrewarmer)
    assert outcome.primed is True
    assert outcome.usage is not None and outcome.usage.total_tokens == 41
    sent = client.ledger.single_request().request
    assert sent.system_prompt == request.system_prompt
    assert sent.messages[:2] == request.messages
    assert (sent.messages[2].role, sent.messages[2].content) == ("user", "ping")
    assert sent.max_output_tokens == 1
    # 原请求未被修改
    assert len(request.messages) == 2
    assert request.max_output_tokens == 2048


async def test_cache_priming_honours_cancellation() -> None:
    client = SimClient(turns=[SimTurn(text="p", usage=_USAGE)])
    cancel = CancellationToken()
    cancel.cancel()

    with pytest.raises(BaseException) as raised:  # noqa: PT011
        await CachePrimingPrewarmer(client).prewarm(
            ApiRequest(model="mock-model"), cancel=cancel
        )

    assert "cancel" in type(raised.value).__name__.lower()
    assert client.ledger.requests() == []


@pytest.mark.parametrize(
    "kwargs", [{"probe_text": "  "}, {"max_output_tokens": 0}],
)
def test_cache_priming_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        CachePrimingPrewarmer(SimClient(turns=[]), **kwargs)


async def test_end_to_end_with_cache_priming(tmp_path: Path) -> None:
    """用会话自己的客户端预热：探针是一次真实采样，但不进 history。"""
    run = _Run()
    client = SimClient(turns=[SimTurn(text="p", usage=_USAGE), _say("回答")])
    run.client = client
    run.pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path), threads_dir=tmp_path / "threads",
        model_client=client, compressors=[],
        model_prewarmer=CachePrimingPrewarmer(client),
    )
    run.engine = await run.pool.get_or_create(session_id="s", entry_skill_id="entry")

    completed = await run.prewarm()
    events = await run.ask()

    assert completed["steps"]["model"] == "primed"
    assert events[-1].msg.kind == "turn_completed"
    probe, first = (record.request for record in client.ledger.requests())
    assert probe.system_prompt == first.system_prompt
    assert [item.kind for item in run.engine.history_snapshot()] == [
        "user_message", "assistant_message",
    ]
    assert "ping" not in str([item.payload for item in run.engine.history_snapshot()])
    await run.pool.close()
