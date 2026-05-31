"""PermissionPolicy + rules + prompter 测试。

覆盖：
    - 既有规则 / 正则 / ask / 记忆机制（原 6 用例）
    - PermissionRequest typed 字段 + 工厂方法（T1）
    - PermissionPolicy.prompter_timeout_seconds（T2）
    - 边界用例（call_chain 截断 / 工厂方法漏字段 / timeout 后不缓存 deny）（T6）
"""

from __future__ import annotations

import anyio
import pytest

from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)
from taifeng.permission.types import CALL_CHAIN_MAX_DEPTH


@pytest.mark.asyncio
async def test_rule_allow_takes_precedence() -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(scope="file_read", target_pattern="/tmp/a", mode="allow"),
        ],
        default_mode="deny",
    )
    d = await policy.check(PermissionRequest(scope="file_read", target="/tmp/a"))
    assert d.granted


@pytest.mark.asyncio
async def test_rule_deny_takes_precedence() -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(scope="shell_exec", target_pattern="re:^rm ", mode="deny"),
        ],
        default_mode="allow",
    )
    d = await policy.check(PermissionRequest(scope="shell_exec", target="rm -rf /"))
    assert not d.granted


@pytest.mark.asyncio
async def test_regex_target_pattern() -> None:
    policy = PermissionPolicy(
        rules=[
            PermissionRule(scope="file_read", target_pattern="re:\\.secret$", mode="deny"),
        ],
        default_mode="allow",
    )
    d_secret = await policy.check(PermissionRequest(scope="file_read", target="/tmp/x.secret"))
    d_ok = await policy.check(PermissionRequest(scope="file_read", target="/tmp/x.txt"))
    assert not d_secret.granted
    assert d_ok.granted


@pytest.mark.asyncio
async def test_ask_mode_with_prompter() -> None:
    called: list[PermissionRequest] = []

    async def cb(req: PermissionRequest) -> PermissionDecision:
        called.append(req)
        return PermissionDecision.allow(reason="test", remember="once")

    policy = PermissionPolicy(default_mode="ask", prompter=CallbackPrompter(cb))
    d = await policy.check(PermissionRequest(scope="custom", target="/something"))
    assert d.granted
    assert len(called) == 1


@pytest.mark.asyncio
async def test_ask_without_prompter_denies() -> None:
    policy = PermissionPolicy(default_mode="ask", prompter=None)
    d = await policy.check(PermissionRequest(scope="custom", target="/anything"))
    assert not d.granted
    assert "ask_mode_no_prompter" in d.reason


@pytest.mark.asyncio
async def test_remember_always_does_not_mutate_rules() -> None:
    """permission-rule-args-match: Taifeng 内核不再自动写回 remember=always 决策。

    decision.remember_until == "always" 仅作信息字段返回；业务侧若想 session
    级记忆，须在 prompter 包装层自管 policy.rules.append（R1 业务零侵入）。
    """
    cb_calls = 0

    async def cb(req: PermissionRequest) -> PermissionDecision:
        nonlocal cb_calls
        cb_calls += 1
        return PermissionDecision.allow(reason="approved", remember="always")

    policy = PermissionPolicy(default_mode="ask", prompter=CallbackPrompter(cb))
    assert len(policy.rules) == 0
    d = await policy.check(PermissionRequest(scope="file_read", target="/tmp/x"))
    assert d.granted
    assert d.remember_until == "always"  # 字段仍正确返回
    # 关键断言：rules 不被自动修改
    assert len(policy.rules) == 0
    assert cb_calls == 1

    # 第二次同 target → 仍走 prompter（没自动学习）
    d2 = await policy.check(PermissionRequest(scope="file_read", target="/tmp/x"))
    assert d2.granted
    assert cb_calls == 2  # 又调了一次（vs 旧行为 cb_calls == 1）


# ====================================================================
# T1: PermissionRequest typed 字段 + 工厂方法
# ====================================================================


def test_permission_request_typed_fields_default() -> None:
    """旧构造保持兼容：不传新字段时 frozen dataclass 仍可构造，字段取默认值。"""
    req = PermissionRequest(scope="tool_use", target="file_read")
    assert req.thread_id == ""
    assert req.submission_id == ""
    assert req.entry_skill_id == ""
    assert req.call_chain == ()
    assert req.turn_index == 0
    assert req.metadata == {}


def test_permission_request_typed_fields_set() -> None:
    """新字段可直接读取，无需走 metadata.get(...) 兜底。"""
    req = PermissionRequest(
        scope="tool_use",
        target="shell_exec",
        thread_id="t1",
        submission_id="s1",
        entry_skill_id="entry-x",
        call_chain=("entry-x", "child-y"),
        turn_index=3,
        metadata={"x_corr": "v-42"},
    )
    assert req.thread_id == "t1"
    assert req.submission_id == "s1"
    assert req.entry_skill_id == "entry-x"
    assert req.call_chain == ("entry-x", "child-y")
    assert req.turn_index == 3
    # 业务侧领域上下文走开放 metadata（无业务命名字段，R1）
    assert req.metadata == {"x_corr": "v-42"}


def test_factory_methods_force_context() -> None:
    """3 个工厂方法签名要求 thread_id / submission_id / entry_skill_id / turn_index 必填。

    运行时漏传 → TypeError；mypy strict 模式下也会拦截（spec 1:1 对应）。
    """
    # for_tool_call 漏关键字 → TypeError
    with pytest.raises(TypeError):
        PermissionRequest.for_tool_call("foo", {})  # type: ignore[call-arg]
    # for_skill_dispatch 漏关键字 → TypeError
    with pytest.raises(TypeError):
        PermissionRequest.for_skill_dispatch("foo")  # type: ignore[call-arg]
    # for_script_exec 漏关键字 → TypeError
    with pytest.raises(TypeError):
        PermissionRequest.for_script_exec("foo", {})  # type: ignore[call-arg]


def test_factory_for_tool_call_constructs_correctly() -> None:
    """for_tool_call 构造结果 scope='tool_use'，typed 字段全部填充。"""
    req = PermissionRequest.for_tool_call(
        "shell_exec",
        {"cmd": "ls"},
        thread_id="t1",
        submission_id="s1",
        entry_skill_id="entry",
        turn_index=2,
    )
    assert req.scope == "tool_use"
    assert req.target == "shell_exec"
    assert req.metadata == {"args": {"cmd": "ls"}}
    assert req.thread_id == "t1"
    assert req.submission_id == "s1"
    assert req.entry_skill_id == "entry"
    assert req.turn_index == 2


def test_factory_extra_metadata_merges_into_metadata() -> None:
    """extra_metadata（业务不透明上下文）原样合并进 metadata，与工厂默认键共存（R1）。"""
    req = PermissionRequest.for_tool_call(
        "shell_exec",
        {"cmd": "ls"},
        thread_id="t1",
        submission_id="s1",
        entry_skill_id="entry",
        turn_index=2,
        extra_metadata={"x_corr": "v-1"},
    )
    assert req.metadata == {"args": {"cmd": "ls"}, "x_corr": "v-1"}


def test_factory_for_skill_dispatch_constructs_correctly() -> None:
    """for_skill_dispatch 构造结果 scope='skill_dispatch'，caller_skill_id 进 metadata。"""
    req = PermissionRequest.for_skill_dispatch(
        "D",
        caller_skill_id="C",
        call_chain=("A", "B", "C"),
        thread_id="t1",
        submission_id="s1",
        entry_skill_id="A",
        turn_index=3,
    )
    assert req.scope == "skill_dispatch"
    assert req.target == "D"
    assert req.call_chain == ("A", "B", "C")
    assert req.entry_skill_id == "A"
    assert req.metadata == {"caller_skill_id": "C"}


def test_factory_for_script_exec_constructs_correctly() -> None:
    """for_script_exec 构造结果 scope='script_exec'。"""
    req = PermissionRequest.for_script_exec(
        "backup.sh",
        {"path": "/tmp"},
        thread_id="t1",
        submission_id="s1",
        entry_skill_id="entry",
        call_chain=("entry",),
        turn_index=1,
    )
    assert req.scope == "script_exec"
    assert req.target == "backup.sh"
    assert req.metadata == {"args": {"path": "/tmp"}}


def test_call_chain_truncated_at_max_depth() -> None:
    """call_chain 超过 32 层时截断到末尾 32 个（保留最近调用栈）。"""
    long_chain = tuple(f"s{i}" for i in range(50))
    req = PermissionRequest(
        scope="skill_dispatch",
        target="x",
        call_chain=long_chain,
    )
    assert len(req.call_chain) == CALL_CHAIN_MAX_DEPTH
    # 末尾保留：最后 32 个 = s18..s49
    assert req.call_chain[0] == f"s{50 - CALL_CHAIN_MAX_DEPTH}"
    assert req.call_chain[-1] == "s49"


# ====================================================================
# T2: PermissionPolicy.prompter_timeout_seconds
# ====================================================================


@pytest.mark.asyncio
async def test_prompter_no_timeout_when_zero() -> None:
    """timeout=0 时 prompter 慢响应仍可正常返回（不退化原行为）。"""

    async def slow_cb(req: PermissionRequest) -> PermissionDecision:
        await anyio.sleep(0.05)  # 50ms < 默认无 timeout
        return PermissionDecision.allow(reason="ok")

    policy = PermissionPolicy(
        default_mode="ask",
        prompter=CallbackPrompter(slow_cb),
        prompter_timeout_seconds=0,
    )
    d = await policy.check(PermissionRequest(scope="custom", target="/x"))
    assert d.granted
    assert d.reason == "ok"


@pytest.mark.asyncio
async def test_prompter_timeout_returns_deny() -> None:
    """CallbackPrompter 睡 5s + timeout=0.1 → 0.5s 内拿到 deny。"""

    async def slow_cb(req: PermissionRequest) -> PermissionDecision:
        await anyio.sleep(5.0)
        return PermissionDecision.allow(reason="should_not_be_returned")

    policy = PermissionPolicy(
        default_mode="ask",
        prompter=CallbackPrompter(slow_cb),
        prompter_timeout_seconds=0.1,
    )
    started = anyio.current_time()
    d = await policy.check(PermissionRequest(scope="custom", target="/x"))
    elapsed = anyio.current_time() - started
    assert not d.granted
    assert "prompter_timeout" in d.reason
    assert elapsed < 0.5, f"超时未生效，用时 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_prompter_timeout_emits_event() -> None:
    """spy telemetry callback 收到 ``permission_prompt_timeout`` 事件。"""

    emitted: list[tuple[str, dict]] = []

    async def telemetry(kind: str, payload: dict) -> None:
        emitted.append((kind, payload))

    async def slow_cb(req: PermissionRequest) -> PermissionDecision:
        await anyio.sleep(5.0)
        return PermissionDecision.allow(reason="x")

    policy = PermissionPolicy(
        default_mode="ask",
        prompter=CallbackPrompter(slow_cb),
        prompter_timeout_seconds=0.05,
        telemetry=telemetry,
    )
    await policy.check(
        PermissionRequest(
            scope="skill_dispatch",
            target="oncology",
            thread_id="t1",
            submission_id="s1",
            call_chain=("entry", "mid"),
        )
    )
    assert len(emitted) == 1
    kind, payload = emitted[0]
    assert kind == "permission_prompt_timeout"
    assert payload["scope"] == "skill_dispatch"
    assert payload["target"] == "oncology"
    assert payload["timeout_seconds"] == 0.05
    assert payload["call_chain"] == ["entry", "mid"]


@pytest.mark.asyncio
async def test_prompter_timeout_does_not_cache_deny() -> None:
    """一次 prompter 超时 deny 后，同 target 再次触发 SHALL 走完整 check 流程。

    spec scenario: ``超时后不重试`` —— 前一次 timeout 不缓存 deny 决策。
    """
    call_count = 0

    async def cb(req: PermissionRequest) -> PermissionDecision:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await anyio.sleep(5.0)
        # 第二次同步返回
        return PermissionDecision.allow(reason="ok2")

    policy = PermissionPolicy(
        default_mode="ask",
        prompter=CallbackPrompter(cb),
        prompter_timeout_seconds=0.05,
    )
    d1 = await policy.check(PermissionRequest(scope="custom", target="/x"))
    assert not d1.granted
    # Taifeng 内核不会自动 append rule（permission-rule-args-match 起统一行为）
    assert len(policy.rules) == 0
    # 第二次再次询问 prompter
    d2 = await policy.check(PermissionRequest(scope="custom", target="/x"))
    assert d2.granted
    assert call_count == 2  # 两次都问了


@pytest.mark.asyncio
async def test_telemetry_callback_exception_isolated() -> None:
    """telemetry callback 抛异常不影响主流程（safe emit）。"""

    async def bad_telemetry(kind: str, payload: dict) -> None:
        raise RuntimeError("telemetry boom")

    async def slow_cb(req: PermissionRequest) -> PermissionDecision:
        await anyio.sleep(5.0)
        return PermissionDecision.allow(reason="x")

    policy = PermissionPolicy(
        default_mode="ask",
        prompter=CallbackPrompter(slow_cb),
        prompter_timeout_seconds=0.05,
        telemetry=bad_telemetry,
    )
    # 应该正常返回 deny，不抛出 RuntimeError
    d = await policy.check(PermissionRequest(scope="custom", target="/x"))
    assert not d.granted
    assert "prompter_timeout" in d.reason
