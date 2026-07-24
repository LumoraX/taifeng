"""Audited 并发 turn 的 hot history 合并与冲突冻结测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import taifeng.loop.engine as engine_module
from taifeng.conversation.models import ResponseItem
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit_history import (
    AuditedHistoryConflictError,
    merge_audited_history,
)
from taifeng.loop.audit_support import SessionAuditFrozenError
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.rewind import RewindCheckpoint
from taifeng.loop.submission import UserMessage
from taifeng.loop.turn import TurnOutcome
from tests.loop.test_audit_submission_admission import _engine_with_audit

if TYPE_CHECKING:
    from pathlib import Path


_CREATED_AT = datetime(2026, 7, 24, 10, 30, tzinfo=UTC)


def _history_item(
    item_id: str,
    *,
    text: str = "safe",
    metadata: dict[str, Any] | None = None,
) -> ResponseItem:
    """构造字段完全确定的 history item。"""
    return ResponseItem(
        id=item_id,
        kind="user_message",
        thread_id="thr_audit_submission",
        payload={"text": text, "attachments": []},
        created_at=_CREATED_AT,
        metadata=metadata or {},
    )


def _conflicting_item(item: ResponseItem, field: str) -> ResponseItem:
    """只改一个完整身份字段，保留相同 item id。"""
    updates: dict[str, object] = {
        "kind": "assistant_message",
        "thread_id": "thr_secret_conflict",
        "payload": {"text": "secret-conflicting-payload"},
        "created_at": _CREATED_AT + timedelta(seconds=1),
        "metadata": {"secret": "raw-metadata"},
    }
    return item.model_copy(update={field: updates[field]})


def test_merge_keeps_distinct_items_and_deduplicates_exact_replays() -> None:
    """current/runner 内 exact replay 均幂等，首次出现顺序保持不变。"""
    first = _history_item("item_first")
    second = _history_item("item_second", text="second")

    merged = merge_audited_history(
        [first, first.model_copy(deep=True)],
        [first.model_copy(deep=True), second, second.model_copy(deep=True)],
    )

    assert merged == [first, second]


@pytest.mark.parametrize(
    "field",
    ["kind", "thread_id", "payload", "created_at", "metadata"],
)
def test_merge_rejects_same_id_with_different_full_content(field: str) -> None:
    """完整身份任一字段不同都不得按 id 静默吞掉。"""
    original = _history_item("item_helper_conflict")

    with pytest.raises(AuditedHistoryConflictError):
        merge_audited_history([original], [_conflicting_item(original, field)])


class _ConflictingRunner:
    """返回可控冲突 history，并携带明显不同的待回写状态。"""

    def __init__(self, history: list[ResponseItem]) -> None:
        """保存 runner 完成后的 history 与状态。"""
        self.history_buffer = history
        self.cache_anchor_index = 777
        self.last_prompt_fingerprint = {"after": "must-not-write"}
        self.compaction_count = 888
        self.total_usage = TokenUsage(total_tokens=999)
        self._seed_pending_call_id: str | None = None

    async def run(self) -> TurnOutcome:
        """返回成功 outcome，使测试只命中 history writeback 边界。"""
        return TurnOutcome(
            success=True,
            iterations=1,
            duration_ms=1,
            usage=self.total_usage,
            final_text="ignored",
            end_reason="completed",
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field",
    ["kind", "thread_id", "payload", "created_at", "metadata"],
)
async def test_audited_history_conflict_freezes_before_runner_state_writeback(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """同 id 任一完整字段冲突都冻结，且不能部分回写 runner 状态。"""
    engine, coordinator, _ = await _engine_with_audit(tmp_path, skills_dir)
    original = _history_item("item_conflict")
    conflict = _conflicting_item(original, field)
    checkpoint = RewindCheckpoint(
        node_id="before",
        turn_index=1,
        kind="iteration",
        history_len=1,
        cache_anchor=4,
        iteration_index=1,
    )
    engine._history = [original]  # noqa: SLF001
    engine._cache_anchor_index = 4  # noqa: SLF001
    engine._rewind_checkpoints = [checkpoint]  # noqa: SLF001
    engine._last_prompt_fingerprint = {"before": "safe"}  # noqa: SLF001
    engine._compaction_count = 3  # noqa: SLF001
    engine._session_tokens = 5  # noqa: SLF001

    def build_runner(**_kwargs: object) -> _ConflictingRunner:
        """替换真实 runner，只控制其完成态 history。"""
        return _ConflictingRunner([original, conflict])

    monkeypatch.setattr(engine_module, "TurnRunner", build_runner)
    with pytest.raises(SessionAuditFrozenError) as raised:
        await engine._build_and_run_runner(  # noqa: SLF001
            "sub_history_conflict",
            CancellationToken(name="history-conflict"),
            [],
        )

    assert raised.value.cause.code == "audit_history_item_conflict"
    assert raised.value.cause.class_name == "AuditedHistoryConflictError"
    assert "secret" not in str(raised.value)
    assert engine._history == [original]  # noqa: SLF001
    assert engine._cache_anchor_index == 4  # noqa: SLF001
    assert engine._rewind_checkpoints == [checkpoint]  # noqa: SLF001
    assert engine._last_prompt_fingerprint == {"before": "safe"}  # noqa: SLF001
    assert engine._compaction_count == 3  # noqa: SLF001
    assert engine._session_tokens == 5  # noqa: SLF001
    snapshot = coordinator.snapshot()
    assert snapshot.first_failure is not None
    assert snapshot.first_failure.code == "audit_history_item_conflict"


async def _wait_until(predicate: object) -> None:
    """在测试 deadline 内等待同步谓词成立。"""
    assert callable(predicate)
    with anyio.fail_after(2):
        while not predicate():
            await anyio.lowlevel.checkpoint()


@pytest.mark.anyio
async def test_reverse_completion_keeps_each_conversation_item_exactly_once(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """后启动 runner 先完成时，两轮 user/assistant 仍各保留一次。"""
    client = RoutingSimClient(
        routes={
            "SECOND_HISTORY_MARK": [
                SimTurn(text="second reply", emit_signal="second-finished")
            ],
            "FIRST_HISTORY_MARK": [
                SimTurn(text="first reply", await_signal="second-finished")
            ],
        }
    )
    engine, coordinator, _ = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    root_cancel = CancellationToken(name="reverse-completion-root")
    actor = asyncio.create_task(engine.run(root_cancel))
    try:
        await engine.submit(UserMessage(text="FIRST_HISTORY_MARK"))
        await _wait_until(lambda: len(client.ledger.requests()) == 1)
        await engine.submit(UserMessage(text="SECOND_HISTORY_MARK"))
        await _wait_until(
            lambda: (
                engine._turn_index == 2  # noqa: SLF001
                and not coordinator.snapshot().accepted_work_ids
            )
        )
    finally:
        root_cancel.cancel()
        await actor

    history = engine.history_snapshot()
    assert len({item.id for item in history}) == len(history)
    texts = [str(item.payload.get("text", "")) for item in history]
    assert texts.count("FIRST_HISTORY_MARK") == 1
    assert texts.count("SECOND_HISTORY_MARK") == 1
    assert texts.count("first reply") == 1
    assert texts.count("second reply") == 1
