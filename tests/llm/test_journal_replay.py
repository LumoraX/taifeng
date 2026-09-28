"""journal-replay —— 用审计 Journal 录制确定性回放 LLM 响应。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import taifeng
from taifeng.conversation.journal.jsonl import JsonlSessionJournalCore
from taifeng.llm.audit import AttemptObservableClientAdapter
from taifeng.llm.providers.replay import (
    JournalReplayClient,
    RecordedCall,
    ReplayDivergenceError,
    ReplayUnsupportedError,
)
from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.loop.audit_config import AuditConfig
from tests.conftest import run_until_root_done_kind

if TYPE_CHECKING:
    from pathlib import Path


def _script() -> list[SimTurn]:
    """父派发子 skill → 子结论 → 父综合（覆盖工具调用 + 子 turn）。"""
    return [
        SimTurn(text="派发风格审查", tool_calls=[{
            "id": "c1", "name": "call_skill",
            "arguments": '{"skill_id": "style-checker", "reason": "审查代码风格"}',
        }]),
        SimTurn(text="风格审查完成：未见违规"),
        SimTurn(text="综合结论：通过"),
    ]


async def _record(tmp_path: Path, skills_dir: Path, prompt: str) -> list[Any]:
    """在 strict audit pool 里跑一轮，返回 Journal 全部已提交记录。"""
    core = JsonlSessionJournalCore(tmp_path / "journal")
    client = AttemptObservableClientAdapter(
        SimClient(turns=_script()), provider="sim", default_model="sim-model")
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=tmp_path / "rec-threads", model_client=client,
        compressors=[], audit=AuditConfig(
            journal_core=core, writer_id="w", max_attachment_bytes=65536,
            max_total_attachment_bytes=1048576))
    engine = await pool.get_or_create(session_id="ses-rec", entry_skill_id="code-reviewer")
    assert await run_until_root_done_kind(engine, taifeng.UserMessage(text=prompt)) == "turn_completed"
    await pool.close()
    return [record async for record in core.load("ses-rec")]


async def _replay(tmp_path: Path, skills_dir: Path, client: JournalReplayClient,
                  prompt: str) -> tuple[str, Any]:
    """用回放客户端在普通 pool 里跑同一输入。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=tmp_path / "replay-threads",
        model_client=client, compressors=[])
    engine = await pool.get_or_create(session_id="ses-replay", entry_skill_id="code-reviewer")
    kind = await run_until_root_done_kind(engine, taifeng.UserMessage(text=prompt))
    history = engine.history_snapshot()
    await pool.close()
    return kind, history


async def test_replay_reproduces_recorded_session(tmp_path: Path, skills_dir: Path) -> None:
    """同一输入回放：全部录制调用按摘要被消费，最终回答与录制一致。"""
    records = await _record(tmp_path, skills_dir, "请审查这段 diff")
    client = JournalReplayClient.from_records(records)
    assert client.remaining == 3

    kind, history = await _replay(tmp_path, skills_dir, client, "请审查这段 diff")

    assert kind == "turn_completed"
    assert client.remaining == 0
    assert len(client.consumed) == 3
    finals = [it.payload["text"] for it in history if it.kind == "assistant_message"]
    assert finals[-1] == "综合结论：通过"


async def test_replay_detects_divergence(tmp_path: Path, skills_dir: Path) -> None:
    """输入不同 → 请求摘要不同 → 显式分叉错误（回归信号），而非静默产出。"""
    records = await _record(tmp_path, skills_dir, "请审查这段 diff")
    client = JournalReplayClient.from_records(records)

    kind, _ = await _replay(tmp_path, skills_dir, client, "换一个问题")

    assert kind == "turn_failed"
    assert client.consumed == []


def _call(items: tuple[dict[str, Any], ...], status: str = "complete") -> RecordedCall:
    return RecordedCall(request_record_id="r1", provider="p", model="m", digest="d" * 64,
                        status=status, normalized_items=items, usage={})


def test_responses_protocol_recording_rejected() -> None:
    """Responses 协议 normalized item（type=message 等）→ 构造期拒绝。"""
    with pytest.raises(ReplayUnsupportedError, match="chat-protocol"):
        JournalReplayClient([_call(({"type": "message", "content": []},))])


def test_unknown_request_raises_divergence() -> None:
    """录制为空时任何请求都分叉。"""
    from taifeng.llm.types import ApiMessage, ApiRequest

    client = JournalReplayClient([])
    with pytest.raises(ReplayDivergenceError):
        client._take(ApiRequest(model="m", messages=[ApiMessage(role="user", content="x")]))  # noqa: SLF001
