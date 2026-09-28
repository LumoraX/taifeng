"""turn 终态事件的用量上报：挂起 / 失败终态也必须带本 turn 已发生的 usage。

背景：此前只有 ``turn_completed`` 携带 ``usage``。turn 在挂起（HITL 等人补料）前
已经完成的模型调用，用量只留在 ``TurnRunner.total_usage`` 里；resume 总是新建
TurnRunner、从零累计，于是挂起前那段用量对事件订阅方（计费 / 审计）永久不可见。
``turn_failed`` 同理：失败前已成功的采样用量同样丢失。

契约：每条 turn 终态事件（completed / suspended / failed）的 ``usage`` 只含**本段**
TurnRunner 的用量；挂起段 + 续跑段相加 = 全部调用用量，互不重复。
"""
from __future__ import annotations

from typing import Any

from taifeng.suspend.record import SuspensionRecord

_TERMINAL_KINDS = ("turn_completed", "turn_failed", "turn_suspended")


async def _drain_until_terminal(engine: Any, sub_id: str) -> Any:
    """订阅 submission 直到第一条 turn 终态事件，返回该终态事件。"""
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in _TERMINAL_KINDS:
            return ev
    raise AssertionError("订阅结束仍未见 turn 终态事件")


def _write_entry_skill(skills_dir: Any, tool_name: str) -> None:
    """写入一个声明 ``tool_name`` 工具的 entry composite skill。"""
    skill_md = f"""---
name: usage-skill
description: usage entry
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: [{tool_name}]
max_call_depth: 2
---
# Usage
"""
    (skills_dir / "usage-skill").mkdir()
    (skills_dir / "usage-skill" / "SKILL.md").write_text(skill_md, encoding="utf-8")


def _tool(name: str) -> Any:
    """构造一个直接返回 ok 的并行安全工具（是否挂起由 permission 策略决定）。"""
    from taifeng.tool.spec import ToolResult, ToolSpec

    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult.ok("done")

    return ToolSpec(
        name=name,
        description="测试用工具",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        parallel_safe=True,
    )


async def test_turn_suspended_usage_reports_pre_suspend_calls_and_resume_not_double_counted(
    skills_dir: Any, threads_dir: Any,
) -> None:
    """等人补料（request_user_input）挂起前的调用用量随 turn_suspended 上报；
    resume 后 turn_completed 只报续跑段。"""
    import taifeng
    from taifeng.llm.providers import SimClient, SimTurn
    from taifeng.llm.types import TokenUsage
    from taifeng.tool.builtins.request_user_input import make_request_user_input_tool

    # Arrange：第一段采样携带完整资料、调 request_user_input（DATA 挂起）；续跑段纯文本收尾
    _write_entry_skill(skills_dir, "request_user_input")
    client = SimClient(turns=[
        SimTurn(
            text="need more data",
            tool_calls=[{"id": "call_rui1", "name": "request_user_input",
                         "arguments": '{"prompt": "请补充化验单"}'}],
            usage=TokenUsage(
                input_tokens=1000, output_tokens=50, total_tokens=1050,
                cache_read_input_tokens=200,
            ),
        ),
        SimTurn(
            text="done",
            usage=TokenUsage(input_tokens=8, output_tokens=4, total_tokens=12),
        ),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
        extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(
        session_id="usage-suspend", entry_skill_id="usage-skill",
    )

    # Act 1：触发挂起
    suspended = await _drain_until_terminal(
        engine, await engine.submit(taifeng.UserMessage(text="go")),
    )

    # Assert 1：挂起终态带上挂起前那次调用的完整用量（含 cache 细分）
    assert suspended.msg.kind == "turn_suspended"
    usage = suspended.msg.data["usage"]
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 50
    assert usage["total_tokens"] == 1050
    assert usage["cache_read_input_tokens"] == 200

    # Act 2：补料后续跑
    items = [it async for it in await pool.store.load_thread(engine.thread_id)]
    rec = SuspensionRecord.from_item(next(it for it in items if it.kind == "suspension"))
    completed = await _drain_until_terminal(engine, await engine.submit(taifeng.Resume(
        thread_id=engine.thread_id,
        resolutions={rec.pending[0].request_id: {"report": "ok"}},
    )))
    await pool.close()

    # Assert 2：续跑段只报自己的用量 —— 两段相加 = 全部调用，无重复计费
    assert completed.msg.kind == "turn_completed"
    assert completed.msg.data["usage"]["input_tokens"] == 8
    assert completed.msg.data["usage"]["output_tokens"] == 4


async def test_turn_failed_usage_reports_calls_before_failure(
    skills_dir: Any, threads_dir: Any,
) -> None:
    """失败前已成功的采样用量随 turn_failed 上报，不因终态失败而丢失。"""
    import taifeng
    from taifeng.llm.providers import SimClient, SimFault, SimTurn
    from taifeng.llm.types import TokenUsage
    from taifeng.loop.failure_policy import FailureContext, FailureDisposition

    class _AlwaysTerminal:
        """把任何失败裁决为终态，确保 turn 以 turn_failed 收尾（而非挂起）。"""

        def decide(self, ctx: FailureContext) -> FailureDisposition:
            return FailureDisposition.TERMINAL

    # Arrange：第一段采样成功并调工具；第二段采样 5xx → 被裁决为终态失败
    _write_entry_skill(skills_dir, "noop")
    client = SimClient(turns=[
        SimTurn(
            text="calling noop",
            tool_calls=[{"id": "call_n1", "name": "noop", "arguments": "{}"}],
            usage=TokenUsage(input_tokens=700, output_tokens=30, total_tokens=730),
        ),
        SimTurn(fault=SimFault.server_error()),
    ])
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        auto_retry=False,  # 只验证终态失败事件本身，不让自动重试改写结局
        failure_policy=_AlwaysTerminal(),
        compressors=[],
        extra_tools=[_tool("noop")],
    )
    engine = await pool.get_or_create(
        session_id="usage-failed", entry_skill_id="usage-skill",
    )

    # Act
    terminal = await _drain_until_terminal(
        engine, await engine.submit(taifeng.UserMessage(text="go")),
    )
    await pool.close()

    # Assert
    assert terminal.msg.kind == "turn_failed", terminal.msg.data
    assert terminal.msg.data["usage"]["input_tokens"] == 700
    assert terminal.msg.data["usage"]["output_tokens"] == 30
