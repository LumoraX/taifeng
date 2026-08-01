"""审计通道引擎级端到端：经真实 AgentEngine 跑完整 turn，断言 durable 落账。

补齐 helper 级测试（test_audit_tool.py / test_audit_skill.py 直接调 audited_tool_batch
/ AuditedSkillDispatch）与引擎级之间的最后一层缺口——**从引擎入口 UserMessage 提交
→ TurnRunner audit 分支 → 真实 JsonlSessionJournalCore durable 落账**的贯通验证。

用真实公有工厂 ``EnginePool.create(audit=...)``：真实内置工具（read_skill / call_skill
按内核默认 offered）、真实 journal core、真实 projector，SimClient 仅替代 provider。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest

import taifeng
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_config import AuditConfig

if TYPE_CHECKING:
    from pathlib import Path


def _observed(client: SimClient) -> AttemptObservableClientAdapter:
    """把 SimClient 包成审计路径要求的官方 attempt-observer adapter。"""
    return AttemptObservableClientAdapter(
        client, provider="sim", default_model="sim-model"
    )


def _audit_config(core: JsonlSessionJournalCore) -> AuditConfig:
    """构造最小合法 strict audit 配置（有界附件上限）。"""
    return AuditConfig(
        journal_core=core,
        writer_id="writer-e2e",
        max_attachment_bytes=65536,
        max_total_attachment_bytes=1048576,
    )


async def _run_until_root_done(
    engine: taifeng.AgentEngine,
    text: str,
    *,
    deadline_seconds: float = 10.0,
) -> str:
    """提交一轮并等**最外层根 turn** 终态，返回 turn_completed / turn_failed。

    call_skill 派生的子 sub-turn 亦发 ``turn_completed``（``is_root=False``），且比父
    entry 更早 emit——``engine.subscribe(sub_id)`` 会在首个 turn_completed 即早退（见
    tests/skill/test_composite_e2e.py 注释）。故改用 ``subscribe_all()`` 后台收集，仅在
    终态事件带 ``is_root=True``（最外层 entry turn）时退出，确保父 turn 完整收敛。
    """
    result: list[str] = []
    done = asyncio.Event()
    sub_holder: list[str] = []

    async def collector() -> None:
        async for ev in engine.subscribe_all():
            if not sub_holder or ev.submission_id != sub_holder[0]:
                continue
            if ev.msg.kind in ("turn_completed", "turn_failed") and ev.msg.data.get(
                "is_root"
            ):
                result.append(ev.msg.kind)
                done.set()
                return

    task = asyncio.create_task(collector())
    await asyncio.sleep(0)  # 让 collector 先注册 subscribe_all 队列
    sub_holder.append(await engine.submit(taifeng.UserMessage(text=text)))
    try:
        await asyncio.wait_for(done.wait(), timeout=deadline_seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    assert result, "未收到根 turn 终态事件"
    return result[0]


@pytest.mark.asyncio
async def test_engine_call_skill_turn_durably_records_tool_and_skill_lineage(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """引擎级 call_skill turn：durable 落账 tool 收敛 + 完整子 skill 谱系。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    client = _observed(
        SimClient(
            turns=[
                # 父 entry：LLM 决定 call_skill 派发子专科 style-checker
                SimTurn(
                    text="派发风格审查子技能",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "call_skill",
                            "arguments": (
                                '{"skill_id": "style-checker", '
                                '"reason": "审查代码风格"}'
                            ),
                        }
                    ],
                ),
                # 子 style-checker（atomic）：一轮文本结论
                SimTurn(text="风格审查完成：未见违规"),
                # 父 entry：拿到子结果后综合
                SimTurn(text="综合审查结论：通过"),
            ]
        )
    )
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        audit=_audit_config(core),
    )
    engine = await pool.get_or_create(
        session_id="ses-e2e", entry_skill_id="code-reviewer"
    )
    assert await _run_until_root_done(engine, "请审查这段 diff") == "turn_completed"
    await pool.close()

    committed = [e async for e in core.load("ses-e2e")]
    types = [e.record_type for e in committed]

    # ---- 存在性：tool 收敛 + 完整子 skill 谱系全部 durable 落账 ----
    # tool 收敛：call_skill 恰一 intent + 恰一 outcome（§8 唯一终态）
    assert types.count("tool_intent_committed") == 1
    assert types.count("tool_outcome_committed") == 1
    # 子 skill 谱系：selected → started 批 → finished 批（§9 全谱系）
    for lineage in (
        "skill_selected",
        "skill_dispatch_started",
        "skill_dispatch_finished",
    ):
        assert types.count(lineage) == 1, lineage
    # 每次 LLM 采样都 checkpoint-before-commit（父2轮 + 子1轮 = 3 次）
    assert types.count("llm_response_checkpoint") == 3
    assert types.count("llm_response_committed") == 3

    # ---- 顺序不变式：审计正确性的核心约束 ----
    def _idx(record_type: str) -> int:
        return types.index(record_type)

    # 意图先于任何效果：tool_intent 先于子 skill 派发
    assert _idx("tool_intent_committed") < _idx("skill_selected")
    # 子 skill 先完成，父 call_skill 才收敛（同步派发语义）
    assert _idx("skill_dispatch_finished") < _idx("tool_outcome_committed")
    # 每个 llm_response_committed 都紧跟其 checkpoint（checkpoint-before-delta）
    committed_idxs = [
        i for i, t in enumerate(types) if t == "llm_response_committed"
    ]
    for ci in committed_idxs:
        assert types[ci - 1] == "llm_response_checkpoint", types[ci - 2 : ci + 1]


@pytest.mark.asyncio
async def test_engine_plain_turn_durably_records_llm_commit_without_tool_records(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """引擎级纯文本 turn：只 durable 落 LLM 提交 + 会话项，无任何 tool/skill 记录。

    锁定审计通道最小 turn 形状（无工具时不得凭空产生 tool_intent/outcome），与
    call_skill 用例互补隔离出「干净采样」路径。
    """
    core = JsonlSessionJournalCore(tmp_path / "journal")
    client = _observed(SimClient(turns=[SimTurn(text="直接回答，无需派发")]))
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        audit=_audit_config(core),
    )
    engine = await pool.get_or_create(
        session_id="ses-plain", entry_skill_id="code-reviewer"
    )
    assert await _run_until_root_done(engine, "你好") == "turn_completed"
    await pool.close()

    types = [e.record_type async for e in core.load("ses-plain")]
    # 干净采样一轮：checkpoint-before-commit 一对
    assert types.count("llm_response_checkpoint") == 1
    assert types.count("llm_response_committed") == 1
    # 无工具/子 skill：绝不凭空产生任何 tool/skill 谱系记录
    for forbidden in (
        "tool_intent_committed",
        "tool_outcome_committed",
        "skill_selected",
        "skill_dispatch_started",
        "skill_dispatch_finished",
    ):
        assert forbidden not in types, forbidden
    # 初始化与收尾骨架齐全
    assert types[:3] == ["session_started", "thread_created", "thread_bound"]
    assert types[-1] == "session_ended"
