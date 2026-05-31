"""instructions-injection 单元 + 边界测试。

涵盖 spec.md 中所有 Requirement 的 Scenario：
    - 数据契约（顶层导入 / frozen / 构造校验）
    - 三档 scope 解析时机
    - 缓存 TTL hit / expire
    - 多层 priority 排序
    - fetch 失败 → InstructionFetchError 上抛
    - 取消传播
    - 热更 UpdateInstructions（替换 / 失效缓存 / 拒绝未知 name）
    - snapshot 不可变
    - Prompt 装配（XML 块 / 顺序 / 空 instructions 不出现块）
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

import taifeng
from taifeng.instructions import (
    InstructionContext,
    InstructionFetchError,
    InstructionLayer,
    InstructionResolver,
    InstructionScope,
    InstructionSource,
    ResolvedInstruction,
)
from taifeng.loop.cancellation import CancellationToken

# ----------------------------------------------------------------------
# T1 数据契约
# ----------------------------------------------------------------------


def test_top_level_imports() -> None:
    """spec Scenario: 顶层导入 —— 五个核心符号 SHALL 可用。"""
    assert taifeng.InstructionSource is InstructionSource
    assert taifeng.InstructionLayer is InstructionLayer
    assert taifeng.InstructionContext is InstructionContext
    assert taifeng.ResolvedInstruction is ResolvedInstruction
    assert taifeng.InstructionFetchError is InstructionFetchError


def test_layer_construction_static_and_dynamic() -> None:
    """T1 Acceptance: 静态字符串 / Protocol 实例两种 source 都能构造。"""
    # 静态
    static = InstructionLayer(
        name="global", source="hello world", scope="engine", priority=10,
    )
    assert static.name == "global"
    assert static.source == "hello world"

    # 动态
    class MySource:
        async def fetch(self, ctx: InstructionContext) -> str | None:
            return "dynamic-text"

    dyn = InstructionLayer(
        name="tenant", source=MySource(), scope="session", priority=50,
    )
    assert dyn.scope == "session"
    assert isinstance(dyn.source, MySource)


def test_layer_negative_ttl_raises() -> None:
    """spec 边界: cache_ttl_seconds < 0 SHALL raise ValueError。"""
    with pytest.raises(ValueError, match="cache_ttl_seconds must be >= 0"):
        InstructionLayer(
            name="x", source="t", scope="engine", cache_ttl_seconds=-1,
        )


def test_layer_frozen() -> None:
    """spec Scenario: dataclass 不可变。"""
    layer = InstructionLayer(name="x", source="t", scope="engine")
    with pytest.raises(dataclasses.FrozenInstanceError):
        layer.name = "y"  # type: ignore[misc]


def test_resolved_frozen() -> None:
    resolved = ResolvedInstruction(
        name="x", scope="engine", text="t",
        fetched_at=1.0, source_kind="static", cache_hit=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.text = "y"  # type: ignore[misc]


def test_instruction_scope_literal() -> None:
    """InstructionScope 是 Literal['engine','session','turn']。"""
    valid: list[InstructionScope] = ["engine", "session", "turn"]
    assert valid == ["engine", "session", "turn"]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _ctx(
    *,
    session_id: str = "s1",
    thread_id: str = "t1",
    entry_skill_id: str = "e1",
    cancel: CancellationToken | None = None,
) -> InstructionContext:
    return InstructionContext(
        session_id=session_id,
        thread_id=thread_id,
        entry_skill_id=entry_skill_id,
        cancel=cancel,
    )


class _Recorder:
    """收集 emit 出的事件 (kind, data) 元组。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, kind: str, data: dict) -> None:
        self.events.append((kind, dict(data)))


class _StaticDynSource:
    """业务侧动态 source —— 每次返回固定文本，可观测调用次数。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def fetch(self, ctx: InstructionContext) -> str | None:
        self.calls += 1
        return self.text


class _FailingSource:
    """业务侧动态 source —— 总是抛指定异常。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def fetch(self, ctx: InstructionContext) -> str | None:
        raise self.exc


class _SlowSource:
    """业务侧动态 source —— sleep 一段时间，模拟 IO；遵守 cancel。"""

    def __init__(self, sleep_s: float) -> None:
        self.sleep_s = sleep_s
        self.entered = asyncio.Event()

    async def fetch(self, ctx: InstructionContext) -> str | None:
        self.entered.set()
        await asyncio.sleep(self.sleep_s)
        return "slow-text"


class _NoneSource:
    """返回 None —— 业务侧主动选择"本次不注入"。"""

    async def fetch(self, ctx: InstructionContext) -> str | None:
        return None


# ----------------------------------------------------------------------
# T2 InstructionResolver
# ----------------------------------------------------------------------


async def test_resolver_static_source_returns_directly() -> None:
    layer = InstructionLayer(
        name="g", source="static-text", scope="engine", priority=10,
    )
    resolver = InstructionResolver([layer])
    out = await resolver.resolve("engine", _ctx())
    assert len(out) == 1
    assert out[0].text == "static-text"
    assert out[0].source_kind == "static"
    assert out[0].cache_hit is False


async def test_resolver_dynamic_source_fetches() -> None:
    src = _StaticDynSource("dyn-text")
    layer = InstructionLayer(
        name="d", source=src, scope="session", priority=20,
    )
    rec = _Recorder()
    resolver = InstructionResolver([layer], emit=rec.emit)
    out = await resolver.resolve("session", _ctx())
    assert out[0].text == "dyn-text"
    assert out[0].source_kind == "dynamic"
    assert src.calls == 1
    kinds = [e[0] for e in rec.events]
    assert "instruction_fetched" in kinds


async def test_resolver_priority_ordering() -> None:
    a = InstructionLayer(name="a", source="A", scope="engine", priority=10)
    b = InstructionLayer(name="b", source="B", scope="engine", priority=50)
    c = InstructionLayer(name="c", source="C", scope="engine", priority=100)
    # 构造时顺序打乱
    resolver = InstructionResolver([b, c, a])
    out = await resolver.resolve("engine", _ctx())
    assert [r.name for r in out] == ["a", "b", "c"]


async def test_resolver_cache_ttl_hit() -> None:
    src = _StaticDynSource("v1")
    layer = InstructionLayer(
        name="d", source=src, scope="session", cache_ttl_seconds=10,
    )
    rec = _Recorder()
    resolver = InstructionResolver([layer], emit=rec.emit)
    ctx = _ctx()
    await resolver.resolve("session", ctx)
    out2 = await resolver.resolve("session", ctx)
    assert src.calls == 1  # 第二次没真调
    assert out2[0].cache_hit is True
    kinds = [e[0] for e in rec.events]
    assert "instruction_cache_hit" in kinds


async def test_resolver_cache_ttl_expired() -> None:
    src = _StaticDynSource("v1")
    layer = InstructionLayer(
        name="d", source=src, scope="session", cache_ttl_seconds=0.05,
    )
    resolver = InstructionResolver([layer])
    ctx = _ctx()
    await resolver.resolve("session", ctx)
    await asyncio.sleep(0.08)  # 等过期
    out2 = await resolver.resolve("session", ctx)
    assert src.calls == 2
    assert out2[0].cache_hit is False


async def test_resolver_dynamic_ttl_zero_each_turn() -> None:
    """ttl=0 + scope=turn → 每次 resolve 必拉。"""
    src = _StaticDynSource("v")
    layer = InstructionLayer(
        name="t", source=src, scope="turn", cache_ttl_seconds=0,
    )
    resolver = InstructionResolver([layer])
    ctx = _ctx()
    await resolver.resolve("turn", ctx)
    await resolver.resolve("turn", ctx)
    await resolver.resolve("turn", ctx)
    assert src.calls == 3


async def test_resolver_fetch_failure_raises_instruction_fetch_error() -> None:
    src = _FailingSource(RuntimeError("db_down"))
    layer = InstructionLayer(name="d", source=src, scope="turn")
    rec = _Recorder()
    resolver = InstructionResolver([layer], emit=rec.emit)
    with pytest.raises(InstructionFetchError) as exc_info:
        await resolver.resolve("turn", _ctx())
    assert exc_info.value.layer_name == "d"
    assert isinstance(exc_info.value.cause, RuntimeError)
    assert "instruction_fetch_failed" in [e[0] for e in rec.events]


async def test_resolver_cancel_propagates_to_fetch() -> None:
    """spec: cancel 时 fetch 抛 CancelledError，SHALL 不发 failed 事件。"""
    cancel = CancellationToken(name="root")
    slow = _SlowSource(sleep_s=10)
    layer = InstructionLayer(name="d", source=slow, scope="turn")
    rec = _Recorder()
    resolver = InstructionResolver([layer], emit=rec.emit)
    ctx = _ctx(cancel=cancel)

    async def _runner() -> None:
        await resolver.resolve("turn", ctx)

    task = asyncio.create_task(_runner())
    await slow.entered.wait()
    cancel.cancel()
    task.cancel()  # 触发 anyio cancel 到正在 sleep 的 fetch
    with pytest.raises(asyncio.CancelledError):
        await task
    # 不应该有 fetch_failed 事件
    assert "instruction_fetch_failed" not in [e[0] for e in rec.events]


async def test_resolver_duplicate_layer_name_raises() -> None:
    a = InstructionLayer(name="x", source="a", scope="engine")
    b = InstructionLayer(name="x", source="b", scope="session")
    with pytest.raises(ValueError, match="duplicate InstructionLayer name"):
        InstructionResolver([a, b])


async def test_resolver_none_text_skips_layer() -> None:
    """fetch 返回 None → ResolvedInstruction 不出现在结果里。"""
    layer = InstructionLayer(
        name="d", source=_NoneSource(), scope="turn",
    )
    resolver = InstructionResolver([layer])
    out = await resolver.resolve("turn", _ctx())
    assert out == []


async def test_resolver_replace_layer_invalidates_cache() -> None:
    src1 = _StaticDynSource("v1")
    layer = InstructionLayer(
        name="d", source=src1, scope="session", cache_ttl_seconds=100,
    )
    resolver = InstructionResolver([layer])
    ctx = _ctx()
    await resolver.resolve("session", ctx)
    assert src1.calls == 1

    # 热更：替换 source
    src2 = _StaticDynSource("v2")
    assert resolver.replace_layer("d", src2) is True
    out = await resolver.resolve("session", ctx)
    assert src2.calls == 1
    assert out[0].text == "v2"


async def test_resolver_replace_unknown_layer_returns_false() -> None:
    resolver = InstructionResolver([
        InstructionLayer(name="x", source="t", scope="engine"),
    ])
    assert resolver.replace_layer("nonexistent", "newval") is False


async def test_resolver_multi_scope_resolve() -> None:
    """resolve 接受 scope 元组，跨档拼接。"""
    a = InstructionLayer(name="a", source="E", scope="engine", priority=10)
    b = InstructionLayer(name="b", source="S", scope="session", priority=20)
    c = InstructionLayer(name="c", source="T", scope="turn", priority=30)
    resolver = InstructionResolver([a, b, c])
    out = await resolver.resolve(("engine", "session", "turn"), _ctx())
    assert [r.name for r in out] == ["a", "b", "c"]


async def test_resolver_text_too_large_raises() -> None:
    """spec 边界: text > 10MB → InstructionFetchError。"""
    big = "x" * (10 * 1024 * 1024 + 1)
    layer = InstructionLayer(name="big", source=big, scope="engine")
    resolver = InstructionResolver([layer])
    with pytest.raises(InstructionFetchError):
        await resolver.resolve("engine", _ctx())


# ----------------------------------------------------------------------
# T4 Engine + EnginePool + Op 集成
# ----------------------------------------------------------------------


from pathlib import Path  # noqa: E402

import taifeng  # noqa: E402
from taifeng.llm.providers.mock import MockClient, MockSession, MockTurn  # noqa: E402
from taifeng.llm.types import ApiRequest, TokenUsage  # noqa: E402


class _CapturingMockClient(MockClient):
    """记录每次 LLM 调用收到的 ApiRequest（用于断言 system prompt 内容）。"""

    def __init__(self, *, turns: list[MockTurn], model: str = "mock-model") -> None:
        super().__init__(turns=turns, model=model)
        self.captured: list[ApiRequest] = []

    def session(self, *, cancel, model=None):  # type: ignore[override]
        sess = super().session(cancel=cancel, model=model)
        sess = _CapturingSession(sess, self)
        return sess


class _CapturingSession:
    """代理 MockSession，把每次 stream 收到的 request 记录下来。"""

    def __init__(self, inner: MockSession, owner: _CapturingMockClient) -> None:
        self._inner = inner
        self._owner = owner

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    async def stream(self, request: ApiRequest):
        self._owner.captured.append(request)
        async for ev in self._inner.stream(request):
            yield ev


async def _drive_one_turn(engine: taifeng.AgentEngine, text: str) -> str:
    """提交一条 UserMessage 并等到 turn_completed / turn_failed。

    Returns:
        最终事件 kind ('turn_completed' / 'turn_failed')。
    """
    sub_id = await engine.submit(taifeng.UserMessage(text=text))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            return ev.msg.kind
    return "unknown"


async def test_engine_uses_instruction_layers(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T4 Acceptance: engine 跑一个 turn，system prompt 含 <system_instructions> 块。"""
    client = _CapturingMockClient(turns=[
        MockTurn(text="ok", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(
                name="policy",
                source="GLOBAL_POLICY_TEXT",
                scope="engine",
                priority=10,
            ),
            InstructionLayer(
                name="tenant",
                source="TENANT_OVERLAY_TEXT",
                scope="session",
                priority=50,
            ),
        ],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    kind = await _drive_one_turn(engine, "hi")
    await pool.close()
    assert kind == "turn_completed"
    # 至少捕获了一次请求
    assert len(client.captured) >= 1
    sys_prompt = client.captured[0].system_prompt[0]
    assert "GLOBAL_POLICY_TEXT" in sys_prompt
    assert "TENANT_OVERLAY_TEXT" in sys_prompt
    # priority 升序：policy(10) 在 tenant(50) 前
    assert sys_prompt.index("GLOBAL_POLICY_TEXT") < sys_prompt.index("TENANT_OVERLAY_TEXT")


async def test_update_instructions_op_hot_swap(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T4 Acceptance: UpdateInstructions 后下个 turn 的 prompt 含新文本。"""
    client = _CapturingMockClient(turns=[
        MockTurn(text="ok1", usage=TokenUsage(input_tokens=10, output_tokens=5)),
        MockTurn(text="ok2", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(
                name="persona", source="OLD_PERSONA", scope="session", priority=10,
            ),
        ],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    # 第一 turn
    await _drive_one_turn(engine, "hi 1")
    # 热更 persona
    update_sub = await engine.submit(
        taifeng.UpdateInstructions(layer_name="persona", new_source="NEW_PERSONA"),
    )
    # 等 instruction_updated 事件
    async for ev in engine.subscribe(update_sub):
        if ev.msg.kind == "instruction_updated":
            assert ev.msg.data["layer_name"] == "persona"
            assert ev.msg.data["new_source_kind"] == "static"
            break
    # 第二 turn 应该看到新文本
    await _drive_one_turn(engine, "hi 2")
    await pool.close()
    assert "OLD_PERSONA" in client.captured[0].system_prompt[0]
    assert "NEW_PERSONA" in client.captured[1].system_prompt[0]


async def test_update_instructions_unknown_layer_rejected(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T4 spec: 未知 name 时 SHALL 发 instruction_update_rejected 事件。"""
    client = _CapturingMockClient(turns=[])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(
                name="real", source="text", scope="engine",
            ),
        ],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    sub_id = await engine.submit(
        taifeng.UpdateInstructions(
            layer_name="nonexistent", new_source="x",
        ),
    )
    seen: str | None = None
    async for ev in engine.subscribe_all():
        if ev.msg.kind == "instruction_update_rejected" and ev.submission_id == sub_id:
            seen = ev.msg.data.get("reason")
            break
        if ev.msg.kind == "shutdown":
            break
    await pool.close()
    assert seen == "unknown_layer"


async def test_snapshot_returns_resolved_after_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T4 Acceptance: snapshot 含 fetched_at / source_kind / cache_volatile。"""
    client = _CapturingMockClient(turns=[
        MockTurn(text="ok", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(name="a", source="A", scope="engine", priority=10),
            InstructionLayer(name="b", source="B", scope="session", priority=20),
        ],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    # 未跑 turn → snapshot 仅含 engine scope 的 'a'
    snap_before = engine.instructions_snapshot()
    names_before = {r.name for r in snap_before}
    assert names_before == {"a"}

    await _drive_one_turn(engine, "hello")
    snap = engine.instructions_snapshot()
    await pool.close()
    names = [r.name for r in snap]
    assert names == ["a", "b"]
    for r in snap:
        assert r.source_kind == "static"
        assert r.fetched_at > 0
        assert isinstance(r.cache_volatile, bool)


async def test_engine_scope_only_resolved_once(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T5 / spec: scope='engine' 的 layer 仅在 EnginePool 构造期 fetch 一次。"""

    class _OnceSource:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, ctx):
            self.calls += 1
            return "ENGINE_TEXT"

    src = _OnceSource()
    client = _CapturingMockClient(turns=[
        MockTurn(text="t1", usage=TokenUsage(input_tokens=10, output_tokens=5)),
        MockTurn(text="t2", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(
                name="engine-layer", source=src, scope="engine",
                cache_ttl_seconds=3600, priority=10,
            ),
        ],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    # warmup 已 fetch 一次
    assert src.calls == 1
    await _drive_one_turn(engine, "hi 1")
    await _drive_one_turn(engine, "hi 2")
    await pool.close()
    # 后续两个 turn 走 cache，calls 还是 1
    assert src.calls == 1


async def test_engine_fetch_failure_fails_turn(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T5 / spec D5: fetch 抛异常 → turn_failed 事件。"""

    class _Boom:
        async def fetch(self, ctx):
            raise RuntimeError("db_down")

    client = _CapturingMockClient(turns=[
        MockTurn(text="ok", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(
                name="bad", source=_Boom(), scope="turn", priority=10,
            ),
        ],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    kind = await _drive_one_turn(engine, "hi")
    await pool.close()
    assert kind == "turn_failed"


# ----------------------------------------------------------------------
# T5 spec-required scenario aliases（与 tasks.md 用例清单一一对应）
# ----------------------------------------------------------------------


async def test_empty_layers_no_block(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T5 #1: 无 layers 时 prompt 与基线完全一致（不含 <system_instructions>）。"""
    client = _CapturingMockClient(turns=[
        MockTurn(text="ok", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=None,
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    await _drive_one_turn(engine, "hello")
    await pool.close()
    assert client.captured
    assert "<system_instructions" not in client.captured[0].system_prompt[0]


async def test_layer_ordering_by_priority_in_engine(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T5 #2: 三层 priority 10/50/100 顺序拼接进 prompt。"""
    client = _CapturingMockClient(turns=[
        MockTurn(text="ok", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(name="C", source="C_TXT", scope="engine", priority=100),
            InstructionLayer(name="A", source="A_TXT", scope="engine", priority=10),
            InstructionLayer(name="B", source="B_TXT", scope="engine", priority=50),
        ],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    await _drive_one_turn(engine, "hi")
    await pool.close()
    sp = client.captured[0].system_prompt[0]
    # A < B < C
    assert sp.index("A_TXT") < sp.index("B_TXT") < sp.index("C_TXT")


async def test_fetch_failed_event_has_cause_repr(
    skills_dir: Path, threads_dir: Path,
) -> None:
    """T5 / spec EventMsg: instruction_fetch_failed SHALL 含 layer_name + cause_repr。"""

    class _Boom:
        async def fetch(self, ctx):
            raise RuntimeError("io_err")

    client = _CapturingMockClient(turns=[
        MockTurn(text="ok", usage=TokenUsage(input_tokens=10, output_tokens=5)),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        instruction_layers=[
            InstructionLayer(name="bad", source=_Boom(), scope="turn"),
        ],
    )
    engine = await pool.get_or_create(
        session_id="s1", entry_skill_id="code-reviewer",
    )
    captured_evt: dict | None = None

    async def _watch():
        nonlocal captured_evt
        async for ev in engine.subscribe_all():
            if ev.msg.kind == "instruction_fetch_failed":
                captured_evt = ev.msg.data
                return
            if ev.msg.kind == "shutdown":
                return

    watch_task = asyncio.create_task(_watch())
    await _drive_one_turn(engine, "hello")
    await asyncio.sleep(0.05)
    await pool.close()
    watch_task.cancel()
    assert captured_evt is not None
    assert captured_evt["layer_name"] == "bad"
    assert "io_err" in captured_evt["cause_repr"]
