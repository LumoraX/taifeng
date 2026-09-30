"""ContextEngine 槽位端到端测试（ADR 0093）：history 不动，只改每次采样发出去的视图。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.context.engine import (
    AssembledContext,
    AssembleRequest,
    TailWindowContextEngine,
    TurnUpdate,
)
from taifeng.context.strategies import SlidingWindowStrategy
from taifeng.conversation.models import system_injection
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.providers.sim import RoutingSimClient
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit_config import (
    AuditCapabilityError,
    AuditStaticInputs,
    _validate_unsupported_fields,
)
from tests.conftest import GUARD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from pathlib import Path

_USAGE = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)

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
# ENTRY_BODY_MARK
"""

_LEAF = """---
name: leaf
description: 叶子
version: 1.0.0
type: atomic
---
# LEAF_BODY_MARK
"""


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    for name, body in (("entry", _ENTRY), ("leaf", _LEAF)):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def _say(text: str) -> SimTurn:
    return SimTurn(text=text, usage=_USAGE)


class _Scripted:
    """按给定函数装配视图的引擎，并记录收到的请求与轮后通知。"""

    name = "scripted"

    def __init__(self, assemble: Any = None, *, after_turn_error: Exception | None = None) -> None:
        self._assemble = assemble
        self._after_turn_error = after_turn_error
        self.requests: list[AssembleRequest] = []
        self.updates: list[TurnUpdate] = []

    async def assemble(self, request: AssembleRequest) -> AssembledContext | None:
        self.requests.append(request)
        return None if self._assemble is None else self._assemble(request)

    async def after_turn(self, update: TurnUpdate) -> None:
        self.updates.append(update)
        if self._after_turn_error is not None:
            raise self._after_turn_error


class _Run:
    """一次测试用到的 pool / engine / client。"""

    def __init__(self) -> None:
        self.pool: taifeng.EnginePool
        self.engine: taifeng.AgentEngine
        self.client: Any

    async def start(self, tmp_path: Path, client: Any, **kwargs: Any) -> None:
        self.client = client
        kwargs.setdefault("compressors", [])
        self.pool = await taifeng.EnginePool.create(
            skills_dir=_skills(tmp_path), threads_dir=tmp_path / "threads",
            model_client=client, **kwargs,
        )
        self.engine = await self.pool.get_or_create(session_id="s", entry_skill_id="entry")

    async def ask(self, text: str) -> list[taifeng.EventMsg]:
        holder: list[str] = []
        events: list[taifeng.EventMsg] = []

        async def collect() -> None:
            depth = 0
            async for event in self.engine.subscribe_all():
                if not holder or event.submission_id != holder[0]:
                    continue
                events.append(event)
                if event.msg.kind == "skill_dispatched":
                    depth += 1
                elif event.msg.kind == "skill_returned":
                    depth -= 1
                if event.msg.kind in ("turn_completed", "turn_failed") and depth <= 0:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        holder.append(await self.engine.submit(taifeng.UserMessage(text=text)))
        await asyncio.wait_for(task, timeout=GUARD_TIMEOUT_SECONDS)
        return events

    def sent(self, index: int = -1) -> list[tuple[str, str]]:
        """某次采样发出的消息（角色, 正文）。"""
        request = self.client.ledger.requests()[index].request
        return [(message.role, str(message.content)) for message in request.messages]

    async def stored(self) -> list[str]:
        items = [i async for i in await self.pool.store.load_thread(self.engine.thread_id)]
        return [item.kind for item in items]


def _data(events: list[taifeng.EventMsg], kind: str) -> list[dict[str, Any]]:
    return [dict(event.msg.data) for event in events if event.msg.kind == kind]


# ====================================================================
# 视图替代完整 history
# ====================================================================


async def test_tail_window_sends_a_view_and_keeps_history(tmp_path: Path) -> None:
    run = _Run()
    await run.start(
        tmp_path, SimClient(turns=[_say("回答一"), _say("回答二"), _say("回答三")]),
        context_engine=TailWindowContextEngine(keep_last_turns=1),
    )

    await run.ask("问题一")
    await run.ask("问题二")
    events = await run.ask("问题三")

    assert events[-1].msg.kind == "turn_completed"
    assert run.sent() == [("user", "问题一"), ("user", "问题三")]
    # 前两轮没超过窗口：原样发送
    assert run.sent(1) == [("user", "问题一"), ("assistant", "回答一"), ("user", "问题二")]
    # history 与落盘内容完整
    kinds = [item.kind for item in run.engine.history_snapshot()]
    assert kinds == ["user_message", "assistant_message"] * 3
    assert await run.stored() == kinds
    assembled = _data(events, "context_assembled")
    assert len(assembled) == 1
    assert assembled[0]["engine"] == "tail_window"
    assert (assembled[0]["history_items"], assembled[0]["view_items"]) == (5, 2)
    assert assembled[0]["cache_invalidated"] is True
    assert assembled[0]["detail"] == {"dropped": 3, "kept_turns": 1}
    await run.pool.close()


async def test_engine_may_send_history_as_is(tmp_path: Path) -> None:
    engine = _Scripted()
    run = _Run()
    await run.start(tmp_path, SimClient(turns=[_say("回答一")]), context_engine=engine)

    events = await run.ask("问题一")

    assert run.sent() == [("user", "问题一")]
    assert _data(events, "context_assembled") == []
    request = engine.requests[0]
    assert (request.thread_id, request.entry_skill_id) == (run.engine.thread_id, "entry")
    assert [item.kind for item in request.history] == ["user_message"]
    assert request.cache_anchor_index == -1
    await run.pool.close()


async def test_view_may_add_items_that_never_enter_history(tmp_path: Path) -> None:
    def assemble(request: AssembleRequest) -> AssembledContext:
        recalled = system_injection(
            text="检索到：上周的结论是方案乙", thread_id=request.thread_id, source="recall",
        )
        return AssembledContext(
            items=[recalled, *request.history], cache_invalidated=True,
            anchor_preserved_until=-1, detail={"recalled": 1},
        )

    run = _Run()
    await run.start(
        tmp_path, SimClient(turns=[_say("回答一")]), context_engine=_Scripted(assemble),
    )

    events = await run.ask("上周定了什么")

    request = run.client.ledger.requests()[0]
    assert "上周的结论是方案乙" in request.blob()
    assert [item.kind for item in run.engine.history_snapshot()] == [
        "user_message", "assistant_message",
    ]
    assert "方案乙" not in str([item.payload for item in run.engine.history_snapshot()])
    assert _data(events, "context_assembled")[0]["detail"] == {"recalled": 1}
    await run.pool.close()


async def test_view_is_assembled_once_per_history_version(tmp_path: Path) -> None:
    engine = _Scripted()
    run = _Run()
    await run.start(tmp_path, SimClient(turns=[_say("回答一")]), context_engine=engine)

    await run.ask("问题一")

    # 预算判定与采样看的是同一份：同一 history 版本只装配一次
    assert len(engine.requests) == 1
    await run.pool.close()


# ====================================================================
# 失败
# ====================================================================


async def test_invalid_view_fails_the_turn(tmp_path: Path) -> None:
    calls = json.dumps({"reason": "需要", "skill_id": "leaf", "args": {}})

    def assemble(request: AssembleRequest) -> AssembledContext | None:
        # 把工具结果丢掉：调用悬空
        items = [item for item in request.history if item.kind != "function_call_output"]
        if len(items) == len(request.history):
            return None
        return AssembledContext(items=items, cache_invalidated=True, anchor_preserved_until=-1)

    run = _Run()
    await run.start(
        tmp_path,
        RoutingSimClient(routes={
            "ENTRY_BODY_MARK": [
                SimTurn(text="派发", usage=_USAGE, tool_calls=[
                    {"id": "tc_leaf", "name": "call_skill", "arguments": calls},
                ]),
                _say("不会走到这里"),
            ],
            "LEAF_BODY_MARK": [_say("leaf 完成")],
        }),
        context_engine=_Scripted(assemble),
    )

    events = await run.ask("开始")

    failed = events[-1]
    assert failed.msg.kind == "turn_failed"
    assert failed.msg.data["kind"] == "ContextEngineError"
    assert "unpaired tool calls" in failed.msg.data["error"]
    # history 没有被引擎的错误视图污染：调用与结果都在
    kinds = [item.kind for item in run.engine.history_snapshot()]
    assert kinds.count("function_call") == kinds.count("function_call_output") == 1
    await run.pool.close()


async def test_engine_exception_fails_the_turn(tmp_path: Path) -> None:
    def assemble(request: AssembleRequest) -> AssembledContext:
        raise RuntimeError("index unavailable")

    run = _Run()
    await run.start(
        tmp_path, SimClient(turns=[_say("回答一")]), context_engine=_Scripted(assemble),
    )

    events = await run.ask("问题一")

    assert events[-1].msg.kind == "turn_failed"
    assert events[-1].msg.data["kind"] == "ContextEngineError"
    assert "RuntimeError: index unavailable" in events[-1].msg.data["error"]
    assert run.client.ledger.requests() == []
    await run.pool.close()


# ====================================================================
# 轮后通知
# ====================================================================


async def test_after_turn_receives_the_new_items(tmp_path: Path) -> None:
    engine = _Scripted()
    run = _Run()
    await run.start(
        tmp_path, SimClient(turns=[_say("回答一"), _say("回答二")]), context_engine=engine,
    )

    await run.ask("问题一")
    await run.ask("问题二")

    # 本轮由 runner 产出的条目；触发本轮的用户消息在 turn 开始前已进 history
    assert [[i.kind for i in update.new_items] for update in engine.updates] == [
        ["assistant_message"], ["assistant_message"],
    ]
    assert [len(update.history) for update in engine.updates] == [2, 4]
    assert {update.thread_id for update in engine.updates} == {run.engine.thread_id}
    await run.pool.close()


async def test_after_turn_failure_is_ignored(tmp_path: Path) -> None:
    engine = _Scripted(after_turn_error=RuntimeError("indexer down"))
    run = _Run()
    await run.start(tmp_path, SimClient(turns=[_say("回答一")]), context_engine=engine)

    events = await run.ask("问题一")

    assert events[-1].msg.kind == "turn_completed"
    assert len(engine.updates) == 1
    await run.pool.close()


# ====================================================================
# 子 skill
# ====================================================================


async def test_child_skill_thread_is_assembled_separately(tmp_path: Path) -> None:
    calls = json.dumps({"reason": "需要", "skill_id": "leaf", "args": {"q": "y"}})
    engine = _Scripted()
    run = _Run()
    await run.start(
        tmp_path,
        RoutingSimClient(routes={
            "ENTRY_BODY_MARK": [
                SimTurn(text="派发", usage=_USAGE, tool_calls=[
                    {"id": "tc_leaf", "name": "call_skill", "arguments": calls},
                ]),
                _say("总结"),
            ],
            "LEAF_BODY_MARK": [_say("leaf 完成")],
        }),
        context_engine=engine,
    )

    events = await run.ask("开始")

    assert events[-1].msg.kind == "turn_completed"
    seen = {(request.thread_id == run.engine.thread_id, request.entry_skill_id)
            for request in engine.requests}
    assert seen == {(True, "entry"), (False, "leaf")}
    assert {update.entry_skill_id for update in engine.updates} == {"entry", "leaf"}
    await run.pool.close()


# ====================================================================
# 预算与压缩看的是视图
# ====================================================================


async def _compaction_attempts(tmp_path: Path, **kwargs: Any) -> int:
    run = _Run()
    # 第二次采样回报的输入量已逼近窗口：完整 history 在第三轮开始时超过软阈值
    heavy = TokenUsage(input_tokens=1100, output_tokens=5, total_tokens=1105)
    await run.start(
        tmp_path,
        SimClient(turns=[_say("回答一"), SimTurn(text="回答二", usage=heavy), _say("回答三")]),
        compressors=[SlidingWindowStrategy()],
        budget=ContextBudget(context_window=1200),
        **kwargs,
    )
    attempts = 0
    for text in ("问题一", "问题二", "问题三"):
        events = await run.ask(text)
        assert events[-1].msg.kind == "turn_completed", events[-1].msg.data
        attempts += len(_data(events, "compaction_started"))
    await run.pool.close()
    return attempts


async def test_view_within_budget_does_not_trigger_compaction(tmp_path: Path) -> None:
    without = await _compaction_attempts(tmp_path / "a")
    with_engine = await _compaction_attempts(
        tmp_path / "b", context_engine=TailWindowContextEngine(keep_last_turns=1),
    )

    # 完整 history 超了软阈值；只发最近一轮的视图没有
    assert without > 0
    assert with_engine == 0


# ====================================================================
# 未启用 / 审计模式
# ====================================================================


async def test_without_engine_and_compressors_there_is_no_orchestrator(tmp_path: Path) -> None:
    run = _Run()
    await run.start(tmp_path, SimClient(turns=[_say("回答一")]))

    assert run.pool._compressors is None  # noqa: SLF001
    events = await run.ask("问题一")
    assert _data(events, "context_assembled") == []
    await run.pool.close()


def test_audit_mode_rejects_context_engine() -> None:
    inputs = AuditStaticInputs(
        model_client=object(),  # type: ignore[arg-type]
        skill_snapshot=object(),  # type: ignore[arg-type]
        failure_suspension_enabled=False,
        skill_suspension_enabled=False,
        context_engine=TailWindowContextEngine(keep_last_turns=1),
    )

    with pytest.raises(AuditCapabilityError) as raised:
        _validate_unsupported_fields(inputs)

    assert raised.value.code == "audit_context_engine_unsupported"
