"""suspend-review-fixes 端到端 —— review(970225a..da93f48)发现缺陷的修复回归。

- spawn 直接 Resume 在飞守卫:异步 store 下并发双 Resume 单结算
- spawn 谱系熔断:auto_retry_count 透传后 max_auto_retries 对 spawn 拓扑生效
- 挂起态拒收新 UserMessage(凭据污染与 engine 级 record 叠加的根除)
- 构造期校验 / TTL 路由失败退避重武装
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.llm.providers.mock import MockTurn, RoutingMockClient
from taifeng.loop.submission import Resume
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

if TYPE_CHECKING:
    from pathlib import Path


def _write(skills: Path, name: str, body: str) -> None:
    d = skills / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


_HOST = """---
name: host
description: 宿主
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [expert]
max_call_depth: 3
---
# 宿主 HOST_MARK
派发专家。
"""

_EXPERT = """---
name: expert
description: 问询专家
version: 1.0.0
type: composite
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 专家 EXPERT_MARK
先问人。
"""


def _expert_turns() -> list[MockTurn]:
    return [
        MockTurn(text="问", tool_calls=[
            {"id": "q1", "name": "request_user_input",
             "arguments": '{"prompt": "补充?"}'}]),
        MockTurn(text="EXPERT_DONE"),
    ]


class _AsyncStore:
    """包装真实 store,每个调用前让出事件循环——模拟异步 MessageStore(业务 DB)。

    JSONL store 同步完成不让出事件循环,会掩盖并发 Resume 的双结算窗口;
    协议(MessageStore)明确支持异步实现,故守卫必须在该形态下也正确。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0)
            return await attr(*args, **kwargs)

        return _wrapped


async def _wait(cond, tries: int = 200) -> bool:
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def _watch(engine, sink: list) -> None:
    async for ev in engine.subscribe_all():
        sink.append(ev.msg)
        if ev.msg.kind == "shutdown":
            return


@pytest.mark.asyncio
async def test_spawn_concurrent_resume_single_settlement_async_store(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """spawn 直接挂起 + 异步 store:并发双 Resume 恰一条胜出——
    单 fco / 单 marker / 单 suspension_resolved,后到者 resolve_in_flight。
    修复前(无在飞守卫):双 fco(不同 payload)+ 双 marker + 双重重建子 runner。"""
    skills = tmp_path / "s"
    _write(skills, "host", _HOST)
    _write(skills, "expert", _EXPERT)
    client = RoutingMockClient(routes={
        "HOST_MARK": [MockTurn(text="host idle")],
        "EXPERT_MARK": _expert_turns(),
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(session_id="sp-race", entry_skill_id="host")
    # 注入异步 store 包装:暴露守卫缺失时的双结算窗口
    engine._store = _AsyncStore(engine._store)  # noqa: SLF001
    events: list = []
    task = asyncio.create_task(_watch(engine, events))
    await asyncio.sleep(0)

    h = await engine.spawn_skill(skill_id="expert", args={}, reason="t")
    hid = h["handle_id"]
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "suspended")
    ctid = h["child_thread_id"]
    items = [it async for it in await pool.store.load_thread(ctid)]
    from taifeng.suspend.record import SuspensionRecord
    rec = SuspensionRecord.from_item(
        [it for it in items if it.kind == "suspension"][-1])
    rid = rec.pending[0].request_id

    # 背靠背并发提交两条不同答案的 Resume
    await engine.submit(Resume(thread_id=ctid, resolutions={rid: {"a": "1"}}))
    await engine.submit(Resume(thread_id=ctid, resolutions={rid: {"a": "2"}}))
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] in ("done", "error"))
    await asyncio.sleep(0.3)  # 留出潜在双结算暴露窗口

    items2 = [it async for it in await pool.store.load_thread(ctid)]
    fcos = [it for it in items2 if it.kind == "function_call_output"
            and it.payload.get("call_id") == "q1"]
    assert len(fcos) == 1, \
        f"并发双 Resume 必须单结算,实得 {len(fcos)} 条 fco: " \
        f"{[it.payload.get('output') for it in fcos]}"
    markers = [it for it in items2 if it.kind == "system_injection"
               and it.payload.get("source") == "suspend_resolved"]
    assert len(markers) == 1, f"resolved-marker 必须恰一条,实得 {len(markers)}"
    resolved = [m for m in events if m.kind == "suspension_resolved"
                and m.data["record_id"] == rec.record_id]
    assert len(resolved) == 1
    rejected = [m for m in events if m.kind == "suspension_resolve_rejected"]
    assert rejected and rejected[0].data["reason"] == "resolve_in_flight", \
        f"后到者应收 resolve_in_flight,实得 {[m.data for m in rejected]}"

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


class _AlwaysFilteredClient(RoutingMockClient):
    """每次采样抛确定性 ContentFilterError(spawn 谱系熔断测试用)。"""

    def __init__(self) -> None:
        super().__init__(routes={})
        self.sample_count = 0

    def session(self, *, cancel: Any, model: str | None = None):  # noqa: ANN201
        outer = self

        class _S:
            async def __aenter__(self):  # noqa: ANN204
                return self

            async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
                pass

            async def stream(self, request):  # noqa: ANN001, ANN201
                from taifeng.llm.errors import ContentFilterError
                outer.sample_count += 1
                raise ContentFilterError("always blocked")
                yield  # pragma: no cover

        return _S()


def _future_now() -> int:
    """真实时间 + 1 小时:任何 ≤3600s 的 ttl 装载即过期。"""
    return int(time.time()) + 3600


@pytest.mark.asyncio
async def test_spawn_auto_retry_lineage_exhaustion(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """spawn 拓扑的谱系熔断(修复前:auto_retry_count 不透传 → 永不熔断,
    复现过 6 秒 528 次无界循环):确定性失败 + on_expire=retry + max=1 →
    恰一次自动 retry 后第二次到期强制 abort,句柄落 error,采样数有界。"""
    skills = tmp_path / "s"
    _write(skills, "host", _HOST)
    _write(skills, "expert", _EXPERT)
    client = _AlwaysFilteredClient()
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        failure_policy=taifeng.SuspendByDefaultPolicy(),
        failure_suspend_ttl_seconds=60,
        failure_suspend_on_expire="retry",
        failure_suspend_max_auto_retries=1,
        now_factory=_future_now,
    )
    engine = await pool.get_or_create(session_id="sp-ex", entry_skill_id="host")
    events: list = []
    task = asyncio.create_task(_watch(engine, events))
    await asyncio.sleep(0)

    h = await engine.spawn_skill(skill_id="expert", args={}, reason="t")
    hid = h["handle_id"]
    assert await _wait(
        lambda: any(m.kind == "suspension_expired"
                    and m.data.get("auto_retry_exhausted") for m in events),
        tries=300), \
        f"应出现熔断标注,实采 {client.sample_count} 次、" \
        f"到期 {sum(1 for m in events if m.kind == 'suspension_expired')} 次"
    assert await _wait(
        lambda: engine.spawn_status([hid])[hid]["status"] == "error")
    await asyncio.sleep(0.3)
    assert client.sample_count == 2, \
        f"熔断后不得继续自动采样(首发 + 1 次 retry),实采 {client.sample_count}"

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


@pytest.mark.asyncio
async def test_user_message_rejected_while_suspended(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """挂起态拒收新 UserMessage:emit TurnFailed(kind=thread_suspended,
    record_id),被拒消息不落史(不污染重放锚/seed);Resume 结清后照常执行。"""
    skills = tmp_path / "s"
    _write(skills, "ask", """---
name: ask
description: 问询
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 问询 ASK_MARK
先问人。
""")
    client = RoutingMockClient(routes={
        "ASK_MARK": [
            MockTurn(text="问", tool_calls=[
                {"id": "q1", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}]),
            MockTurn(text="ASK_DONE"),
            MockTurn(text="SECOND_DONE"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(session_id="um-rej", entry_skill_id="ask")
    events: list = []
    task = asyncio.create_task(_watch(engine, events))
    await asyncio.sleep(0)

    await engine.submit(taifeng.UserMessage(text="第一问"))
    assert await _wait(lambda: any(m.kind == "turn_suspended" for m in events))
    susp = next(m for m in events if m.kind == "turn_suspended")
    rid = susp.data["pending"][0]["request_id"]

    # 挂起中提交第二条 → 显式拒绝,不落史
    await engine.submit(taifeng.UserMessage(text="第二问"))
    assert await _wait(lambda: any(
        m.kind == "turn_failed" and m.data.get("kind") == "thread_suspended"
        for m in events)), "挂起中新 UserMessage 应被显式拒绝"
    rej = next(m for m in events if m.kind == "turn_failed"
               and m.data.get("kind") == "thread_suspended")
    assert rej.data["record_id"] == susp.data["record_id"]
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    texts = [str(it.payload.get("text", "")) for it in items
             if it.kind == "user_message"]
    assert texts == ["第一问"], f"被拒消息不得落史,实得 {texts}"

    # Resume 结清 → 续跑完成;再提交照常执行
    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={rid: {"answer": "好"}}))
    assert await _wait(lambda: any(
        m.kind == "turn_completed" and m.data.get("is_root") for m in events))
    await engine.submit(taifeng.UserMessage(text="第三问"))
    assert await _wait(lambda: sum(
        1 for m in events
        if m.kind == "turn_completed" and m.data.get("is_root")) >= 2), \
        "结清后新消息应照常执行"

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def test_pool_ctor_rejects_bad_limits(tmp_path: Path) -> None:
    """构造期校验:max_session_tokens ≤ 0 与 failure_suspend_max_auto_retries ≤ 0
    在 pool 构造点即 ValueError(报错点贴近配置点)。"""
    skills = tmp_path / "s"
    _write(skills, "host", _HOST)
    _write(skills, "expert", _EXPERT)
    threads = tmp_path / "t"
    threads.mkdir()
    with pytest.raises(ValueError, match="max_session_tokens"):
        await taifeng.EnginePool.create(
            skills_dir=skills, threads_dir=threads,
            model_client=RoutingMockClient(routes={}), compressors=[],
            max_session_tokens=0)
    with pytest.raises(ValueError, match="failure_suspend_max_auto_retries"):
        await taifeng.EnginePool.create(
            skills_dir=skills, threads_dir=threads,
            model_client=RoutingMockClient(routes={}), compressors=[],
            failure_suspend_max_auto_retries=0)


@pytest.mark.asyncio
async def test_ttl_unroutable_rearms_instead_of_dropping(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """TTL 到期路由解析失败 → 退避重武装(修复前直接放弃,该到期永久丢失)。
    以单次路由失败注入验证:第二轮武装后裁决正常投递。"""
    skills = tmp_path / "s"
    _write(skills, "ask", """---
name: ask
description: 问询
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 问询 ASK_MARK
先问人。
""")
    client = RoutingMockClient(routes={
        "ASK_MARK": [
            MockTurn(text="问", tool_calls=[
                {"id": "q1", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}]),
            MockTurn(text="不应被采样"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool(ttl_seconds=60)],
        now_factory=_future_now,
    )
    engine = await pool.get_or_create(session_id="ttl-rearm", entry_skill_id="ask")
    # 注入:前 25 次路由解析强制失败——覆盖整个首轮 fire 的 20 次内置重试,
    # 确保走到「重试耗尽 → 退避重武装」路径(修复前此处直接放弃,测试超时红)
    real_resolve = engine._resolve_expiry_route  # noqa: SLF001
    fail_once = {"n": 0}

    async def _flaky_route(thread_id: str, record_id: str):
        if fail_once["n"] < 25:
            fail_once["n"] += 1
            return None
        return await real_resolve(thread_id, record_id)

    engine._resolve_expiry_route = _flaky_route  # noqa: SLF001
    events: list = []
    task = asyncio.create_task(_watch(engine, events))
    await asyncio.sleep(0)

    await engine.submit(taifeng.UserMessage(text="开始"))
    # 首轮 fire:20 次重试全部失败 → 重武装;约 2s 后第二轮投递成功
    assert await _wait(lambda: any(
        m.kind == "suspension_resolved" for m in events), tries=400), \
        "路由失败后应重武装并最终投递裁决(而非永久丢失)"
    assert fail_once["n"] >= 1, "注入的首轮失败应被触发"

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


@pytest.mark.asyncio
async def test_resume_passes_k2_gate_conservative_documented(
    tmp_path: Path, threads_dir: Path,
) -> None:
    """Resume 续跑过 K2 闸门(对 Conservative 是行为变化,本用例钉住声明):
    会话已触顶时,HITL 答复被消费(gap 回填)但续跑被闸 → ResourceLimitExceeded
    (turn_refused) + TurnFailed,不再静默烧 token。"""
    skills = tmp_path / "s"
    _write(skills, "ask", """---
name: ask
description: 问询
version: 1.0.0
type: composite
entry: true
model: mock-model
tool_names: [request_user_input]
max_call_depth: 2
---
# 问询 ASK_MARK
先问人。
""")
    from taifeng.llm.types import TokenUsage
    client = RoutingMockClient(routes={
        "ASK_MARK": [
            MockTurn(text="问", tool_calls=[
                {"id": "q1", "name": "request_user_input",
                 "arguments": '{"prompt": "补充?"}'}],
                usage=TokenUsage(input_tokens=200, total_tokens=200)),
            MockTurn(text="不应被采样"),
        ],
    })
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()],
        max_session_tokens=100,  # 首 turn 即超限
    )
    engine = await pool.get_or_create(session_id="k2-gate", entry_skill_id="ask")
    events: list = []
    task = asyncio.create_task(_watch(engine, events))
    await asyncio.sleep(0)

    await engine.submit(taifeng.UserMessage(text="开始"))
    assert await _wait(lambda: any(m.kind == "turn_suspended" for m in events))
    susp = next(m for m in events if m.kind == "turn_suspended")
    rid = susp.data["pending"][0]["request_id"]

    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={rid: {"answer": "好"}}))
    assert await _wait(lambda: any(
        m.kind == "turn_failed"
        and m.data.get("kind") == "resource_limit_exceeded" for m in events)), \
        "已触顶会话的续跑应被 K2 闸门拦下(Conservative → 终态)"
    assert any(m.kind == "resource_limit_exceeded"
               and m.data["scope"] == "turn_refused" for m in events)

    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()
