"""memory 内置工具测试（ADR 0064）：动作委托 / 参数校验 / 结果截断 / store 抛错显式返回 /
取消 / 按动作的副作用分类 / schema，以及未注册不可见与启用后端到端调用。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.context.memory import ForgettableMemoryStore, NullMemoryStore
from taifeng.llm.providers import SimTurn
from taifeng.loop.audit_config import AUDIT_TOOL_EFFECT_RECONCILIATION
from taifeng.loop.cancellation import CancellationToken
from taifeng.tool.arg_validation import check_tool_arguments
from taifeng.tool.builtins import make_memory_tool
from taifeng.tool.builtins.memory import MEMORY_TOOL_SOURCE
from taifeng.tool.spec import ToolContext
from tests.conftest import wait_for_condition

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from taifeng.conversation.models import ResponseItem


def _ctx(token: CancellationToken | None = None) -> ToolContext:
    """最小工具上下文。"""
    return ToolContext(call_id="call_m", cancel=token or CancellationToken(), thread_id="t1")


class _RecordingStore(NullMemoryStore):
    """记录委托调用的内存 store：prefetch 返回预置文本，writeback 收集 items。"""

    def __init__(self, recall: str = "") -> None:
        self.recall = recall
        self.queries: list[tuple[str, str]] = []
        self.written: list[tuple[str, list[ResponseItem]]] = []

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        self.queries.append((query, thread_id))
        return self.recall

    async def writeback(self, *, thread_id: str, items: Sequence[ResponseItem]) -> None:
        self.written.append((thread_id, list(items)))


class _BrokenStore(NullMemoryStore):
    """后端故障替身：两条委托路径都抛错。"""

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        raise ConnectionError("vector db down")

    async def writeback(self, *, thread_id: str, items: Sequence[ResponseItem]) -> None:
        raise ConnectionError("vector db down")


async def test_search_delegates_prefetch_with_thread_scope() -> None:
    """search → prefetch(query, thread_id=当前 thread)，返回文本原样给模型。"""
    store = _RecordingStore(recall="用户偏好：回答用中文")
    r = await make_memory_tool(store).handler({"action": "search", "query": "偏好"}, _ctx())
    assert not r.is_error
    assert r.output == "用户偏好：回答用中文"
    assert store.queries == [("偏好", "t1")]


async def test_search_empty_and_truncated() -> None:
    """空结果是正常结果；超长按字符截断并注明。"""
    empty = await make_memory_tool(_RecordingStore()).handler(
        {"action": "search", "query": "q"}, _ctx(),
    )
    assert not empty.is_error
    assert empty.output == "no relevant memory found"
    long = await make_memory_tool(_RecordingStore(recall="x" * 50), max_result_chars=10).handler(
        {"action": "search", "query": "q"}, _ctx(),
    )
    assert long.output.startswith("x" * 10 + "\n\n[memory result truncated to 10 chars]")
    assert long.data["truncated"] is True
    assert long.data["chars"] == 50


async def test_save_delegates_writeback_with_marked_item() -> None:
    """save → writeback 一条 assistant_message，metadata 标来源与 call_id。"""
    store = _RecordingStore()
    r = await make_memory_tool(store).handler(
        {"action": "save", "content": "项目约定：日期统一 ISO 8601"}, _ctx(),
    )
    assert not r.is_error
    assert r.output == "saved to memory (18 chars)"
    [(thread_id, items)] = store.written
    assert thread_id == "t1"
    [item] = items
    assert item.kind == "assistant_message"
    assert item.thread_id == "t1"
    assert item.payload["text"] == "项目约定：日期统一 ISO 8601"
    assert item.metadata == {"source": MEMORY_TOOL_SOURCE, "call_id": "call_m"}


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"action": "search"}, "bad_args"),
        ({"action": "search", "query": "   "}, "bad_args"),
        ({"action": "search", "query": "q" * 2001}, "bad_args"),
        ({"action": "save"}, "bad_args"),
        ({"action": "save", "content": ""}, "bad_args"),
        ({"action": "save", "content": "x" * 21}, "too_large"),
        ({"action": "delete"}, "bad_args"),
        ({}, "bad_args"),
    ],
)
async def test_bad_args_rejected_without_touching_store(
    args: dict[str, Any], reason: str,
) -> None:
    """缺参 / 空白 / 超长 / 未知动作显式拒绝，store 零触达（超长要点不截断写入）。"""
    store = _RecordingStore()
    r = await make_memory_tool(store, max_save_chars=20).handler(args, _ctx())
    assert r.is_error
    assert r.data["reason"] == reason
    assert store.queries == []
    assert store.written == []


@pytest.mark.parametrize(
    "args", [{"action": "search", "query": "q"}, {"action": "save", "content": "c"}],
)
async def test_store_exception_is_returned_explicitly(args: dict[str, Any]) -> None:
    """后端抛错 → is_error 的 memory_error（带异常类型与消息），不吞成空结果。"""
    r = await make_memory_tool(_BrokenStore()).handler(args, _ctx())
    assert r.is_error
    assert r.data["reason"] == "memory_error"
    assert "ConnectionError: vector db down" in r.output
    assert r.data["action"] == args["action"]


async def test_cancel_interrupts_slow_backend() -> None:
    """后端阻塞时 token 取消 → 原地打断，返回 cancelled 结果（R4）。"""
    entered = asyncio.Event()

    class _SlowStore(NullMemoryStore):
        """prefetch 永不返回，模拟卡住的后端。"""

        async def prefetch(self, query: str, *, thread_id: str) -> str:
            entered.set()
            await asyncio.Event().wait()
            return ""

    token = CancellationToken()
    task = asyncio.create_task(
        make_memory_tool(_SlowStore()).handler({"action": "search", "query": "q"}, _ctx(token)),
    )
    await wait_for_condition(entered.is_set)
    token.cancel()
    r = await task
    assert r.is_error
    assert r.data["reason"] == "cancelled"


async def test_pre_cancelled_skips_store() -> None:
    """已取消的 token → 不调 store 直接 cancelled。"""
    store = _RecordingStore()
    token = CancellationToken()
    token.cancel()
    r = await make_memory_tool(store).handler({"action": "search", "query": "q"}, _ctx(token))
    assert r.data["reason"] == "cancelled"
    assert store.queries == []


def test_effect_classification_follows_enabled_actions() -> None:
    """含 save 取最保守一档（串行 + external_non_idempotent/manual）；仅 search 为只读 pure。"""
    rw = make_memory_tool(_RecordingStore())
    assert rw.parallel_safe is False
    assert (rw.effect_kind, rw.reconciliation) == ("external_non_idempotent", "manual")
    ro = make_memory_tool(_RecordingStore(), actions=("search",))
    assert ro.parallel_safe is True
    assert (ro.effect_kind, ro.reconciliation) == ("pure", "none")
    # 两档组合都是 ADR 0025 合法组合（strict audit 可接受）
    for spec in (rw, ro):
        assert (spec.effect_kind, spec.reconciliation) in AUDIT_TOOL_EFFECT_RECONCILIATION


async def test_search_only_tool_hides_save() -> None:
    """只读装配：schema 不暴露 save / content，调用 save 以 bad_args 拒绝、不触达 writeback。"""
    store = _RecordingStore()
    spec = make_memory_tool(store, actions=("search",))
    assert spec.input_schema["properties"]["action"]["enum"] == ["search"]
    assert "content" not in spec.input_schema["properties"]
    assert "save" not in spec.description
    r = await spec.handler({"action": "save", "content": "x"}, _ctx())
    assert r.data["reason"] == "bad_args"
    assert store.written == []


def test_schema_rejects_unknown_args_and_foreign_action() -> None:
    """additionalProperties=false：未知参数、未启用动作在派发前被拒。"""
    spec = make_memory_tool(_RecordingStore())
    assert check_tool_arguments(spec.input_schema, {"action": "search", "top_k": 3}) is not None
    assert check_tool_arguments(spec.input_schema, {"action": "forget"}) is not None
    assert check_tool_arguments(spec.input_schema, {"action": "save", "content": "c"}) is None


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"actions": ()}, "must not be empty"),
        ({"actions": ("search", "update")}, "unsupported memory actions"),
        ({"actions": ("search", "delete")}, "requires a store implementing ForgettableMemoryStore"),
        ({"max_result_chars": 0}, "must be > 0"),
    ],
)
def test_factory_rejects_bad_configuration(kwargs: dict[str, Any], fragment: str) -> None:
    """装配期错误（空动作集 / 未知动作 / 不可遗忘却启用 delete / 非正上限）构造时即报错。"""
    with pytest.raises(ValueError, match=fragment):
        make_memory_tool(_RecordingStore(), **kwargs)


# ── delete（可选协议 ForgettableMemoryStore，ADR 0071）─────────────────────────


class _ForgetStore(_RecordingStore):
    """实现 forget 的 store：记录删除依据，返回预置计数或抛预置异常。"""

    def __init__(self, deleted: Any = 1, boom: Exception | None = None) -> None:
        super().__init__()
        self.deleted = deleted
        self.boom = boom
        self.forgotten: list[tuple[str, str]] = []

    async def forget(self, target: str, *, thread_id: str) -> int:
        self.forgotten.append((target, thread_id))
        if self.boom is not None:
            raise self.boom
        return self.deleted  # type: ignore[no-any-return]


def test_delete_offered_only_for_forgettable_store() -> None:
    """缺省动作集随 store 能力：可遗忘才出现 delete（schema 与描述同步）；否则不出现。"""
    assert isinstance(_ForgetStore(), ForgettableMemoryStore)
    assert not isinstance(_RecordingStore(), ForgettableMemoryStore)
    spec = make_memory_tool(_ForgetStore())
    assert spec.input_schema["properties"]["action"]["enum"] == ["search", "save", "delete"]
    assert "target" in spec.input_schema["properties"]
    assert "action=delete" in spec.description
    plain = make_memory_tool(_RecordingStore())
    assert plain.input_schema["properties"]["action"]["enum"] == ["search", "save"]
    assert "target" not in plain.input_schema["properties"]
    assert "delete" not in plain.description
    opted_out = make_memory_tool(_ForgetStore(), actions=("search", "save"))
    assert "target" not in opted_out.input_schema["properties"]


def test_delete_effect_classification() -> None:
    """delete 是改动后端的动作：只要启用就取最保守一档（ADR 0025 合法组合）。"""
    spec = make_memory_tool(_ForgetStore(), actions=("search", "delete"))
    assert spec.parallel_safe is False
    assert (spec.effect_kind, spec.reconciliation) == ("external_non_idempotent", "manual")
    assert (spec.effect_kind, spec.reconciliation) in AUDIT_TOOL_EFFECT_RECONCILIATION


async def test_delete_delegates_forget_and_reports_count() -> None:
    """delete → forget(target, thread_id=当前 thread)，返回实际删除条数。"""
    store = _ForgetStore(deleted=2)
    r = await make_memory_tool(store).handler({"action": "delete", "target": "[mem:7]"}, _ctx())
    assert not r.is_error
    assert r.output == "deleted 2 memory record(s)"
    assert r.data["deleted"] == 2
    assert store.forgotten == [("[mem:7]", "t1")]


async def test_delete_zero_is_normal_result() -> None:
    """没有匹配不是错误，明确告知未删除。"""
    r = await make_memory_tool(_ForgetStore(deleted=0)).handler(
        {"action": "delete", "target": "不存在的记忆"}, _ctx(),
    )
    assert not r.is_error
    assert r.output == "no matching memory found; nothing deleted"


@pytest.mark.parametrize("bad", [-1, True, "2", None])
async def test_delete_invalid_count_is_memory_error(bad: Any) -> None:
    """后端返回非法计数（负数 / bool / 非整数）→ 显式 memory_error，不当作成功。"""
    r = await make_memory_tool(_ForgetStore(deleted=bad)).handler(
        {"action": "delete", "target": "x"}, _ctx(),
    )
    assert r.is_error
    assert r.data["reason"] == "memory_error"
    assert "invalid count" in r.output


@pytest.mark.parametrize(
    "args",
    [{"action": "delete"}, {"action": "delete", "target": "  "},
     {"action": "delete", "target": "t" * 2001}, {"action": "delete", "target": 3}],
)
async def test_delete_bad_args_do_not_touch_store(args: dict[str, Any]) -> None:
    """缺 target / 空白 / 超长 / 类型错 → bad_args，forget 零触达。"""
    store = _ForgetStore()
    r = await make_memory_tool(store).handler(args, _ctx())
    assert r.data["reason"] == "bad_args"
    assert store.forgotten == []


async def test_delete_backend_failure_is_explicit() -> None:
    """forget 抛错 → memory_error 带异常类型与消息。"""
    r = await make_memory_tool(_ForgetStore(boom=PermissionError("read-only replica"))).handler(
        {"action": "delete", "target": "x"}, _ctx(),
    )
    assert r.is_error
    assert r.data == {"reason": "memory_error", "action": "delete"}
    assert "PermissionError: read-only replica" in r.output


async def test_delete_cancel_interrupts_slow_backend() -> None:
    """forget 阻塞时 token 取消 → 原地打断返回 cancelled（R4）。"""
    entered = asyncio.Event()

    class _SlowForget(_ForgetStore):
        """forget 永不返回。"""

        async def forget(self, target: str, *, thread_id: str) -> int:
            entered.set()
            await asyncio.Event().wait()
            return 0

    token = CancellationToken()
    task = asyncio.create_task(
        make_memory_tool(_SlowForget()).handler({"action": "delete", "target": "x"}, _ctx(token)),
    )
    await wait_for_condition(entered.is_set)
    token.cancel()
    r = await task
    assert r.data["reason"] == "cancelled"


# ── 端到端：真实 EnginePool + SimClient ─────────────────────────────────────


_SKILL = """---
name: assistant
description: 带长期记忆的助手
version: 1.0.0
type: composite
entry: true
tool_names: [memory]
max_call_depth: 2
---
# MEMORY_MARK 记得用户的偏好
"""


def _write_skill(tmp_path: Path) -> Path:
    """写入声明了 memory 工具的 composite entry skill，返回 skills 目录。"""
    skills = tmp_path / "skills"
    (skills / "assistant").mkdir(parents=True)
    (skills / "assistant" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return skills


async def _run_turn(engine: taifeng.AgentEngine, text: str) -> None:
    """提交一条用户消息并等到该 turn 完成。"""
    sub = await engine.submit(taifeng.UserMessage(text=text))
    async for ev in engine.subscribe(sub):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            assert ev.msg.kind == "turn_completed"
            return


async def test_memory_tool_invisible_when_not_registered(
    tmp_path: Path, threads_dir: Path, sim_client,
) -> None:
    """opt-in：skill 声明了 memory 但未经 extra_tools 注册 → 请求里不可见。"""
    client = sim_client(turns=[SimTurn(text="好的")])
    pool = await taifeng.EnginePool.create(
        skills_dir=_write_skill(tmp_path), threads_dir=threads_dir,
        model_client=client, compressors=[], memory_store=_RecordingStore(),
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="assistant")
        await _run_turn(engine, "你好")
        assert "memory" not in client.ledger.requests()[0].tool_names()
    finally:
        await pool.close()


async def test_memory_tool_e2e_save_via_engine_pool(
    tmp_path: Path, threads_dir: Path, sim_client,
) -> None:
    """同一 store 双注入：LLM 调一次 memory(save)，store 收到带标记的要点，工具结果回流。"""
    store = _RecordingStore()
    client = sim_client(turns=[
        SimTurn(text="记下来", tool_calls=[{
            "id": "m1", "name": "memory",
            "arguments": json.dumps({"action": "save", "content": "用户偏好简洁回答"}),
        }]),
        SimTurn(text="已记住"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=_write_skill(tmp_path), threads_dir=threads_dir,
        model_client=client, compressors=[],
        memory_store=store, extra_tools=[make_memory_tool(store)],
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="assistant")
        await _run_turn(engine, "以后回答简洁点")
        assert "memory" in client.ledger.requests()[0].tool_names()
        explicit = [
            it for _, items in store.written for it in items
            if it.metadata.get("source") == MEMORY_TOOL_SOURCE
        ]
        assert [it.payload["text"] for it in explicit] == ["用户偏好简洁回答"]
        assert explicit[0].metadata["call_id"] == "m1"
        [output] = [
            it.payload for it in engine.history_snapshot()
            if it.kind == "function_call_output" and it.payload["call_id"] == "m1"
        ]
        assert output["is_error"] is False
        assert output["output"] == "saved to memory (8 chars)"
    finally:
        await pool.close()


async def test_memory_tool_e2e_delete_via_engine_pool(
    tmp_path: Path, threads_dir: Path, sim_client,
) -> None:
    """可遗忘 store：请求里 memory 的 schema 含 delete，LLM 调一次 delete，结果回流。"""
    store = _ForgetStore(deleted=1)
    client = sim_client(turns=[
        SimTurn(text="删掉", tool_calls=[{
            "id": "d1", "name": "memory",
            "arguments": json.dumps({"action": "delete", "target": "[mem:3]"}),
        }]),
        SimTurn(text="已删除"),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=_write_skill(tmp_path), threads_dir=threads_dir,
        model_client=client, compressors=[],
        memory_store=store, extra_tools=[make_memory_tool(store)],
    )
    try:
        engine = await pool.get_or_create(session_id="s", entry_skill_id="assistant")
        await _run_turn(engine, "忘掉那条过时的约定")
        [tool] = [t for t in client.ledger.requests()[0].request.tools if t.name == "memory"]
        assert "delete" in tool.input_schema["properties"]["action"]["enum"]
        assert store.forgotten[0][0] == "[mem:3]"
        [output] = [
            it.payload for it in engine.history_snapshot()
            if it.kind == "function_call_output" and it.payload["call_id"] == "d1"
        ]
        assert output["output"] == "deleted 1 memory record(s)"
    finally:
        await pool.close()
