"""CancelTurn lifecycle 与 cancellation-origin 审查回归。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from taifeng.conversation.journal import SubmissionAppliedV1
from taifeng.llm.events import completed
from taifeng.llm.types import TokenUsage
from taifeng.loop.audit import (
    AuditHealth,
    SessionFinishingError,
)
from taifeng.loop.cancellation import CancellationToken
from taifeng.loop.submission import CancelTurn, UserMessage
from tests.loop.test_audit_cancel_turn import (
    _load_journal,
    _stop_actor,
    _wait_for_record,
)
from tests.loop.test_audit_submission_admission import _engine_with_audit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from taifeng.llm.events import ResponseEvent
    from taifeng.llm.types import ApiRequest


class _RootFirstSession:
    """先观测 Session root 取消，再由测试释放 runner 收敛。"""

    def __init__(
        self,
        client: _RootFirstClient,
        cancel: CancellationToken,
    ) -> None:
        """保存 root-first 竞态控制器与 target token。"""
        self._client = client
        self._cancel = cancel

    async def __aenter__(self) -> _RootFirstSession:
        """返回当前 session。"""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """无额外资源需要释放。"""

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """root 先取消 target；barrier 保持 target active 供 late CancelTurn 竞争。"""
        del request
        self._client.target_entered.set()
        await self._cancel.wait_cancelled()
        self._client.root_cancel_observed.set()
        await self._client.release_target.wait()
        self._cancel.raise_if_cancelled()
        if False:
            yield completed(response_id=None, usage=TokenUsage(), end_turn=True)


class _RootFirstClient:
    """控制 root-first cancellation attribution 的确定性竞态。"""

    def __init__(self) -> None:
        """初始化 target、root-cancel 与 runner-release barriers。"""
        self.target_entered = anyio.Event()
        self.root_cancel_observed = anyio.Event()
        self.release_target = anyio.Event()

    def session(
        self,
        *,
        cancel: CancellationToken,
        model: str | None = None,
    ) -> _RootFirstSession:
        """返回绑定 target token 的受控 session。"""
        del model
        return _RootFirstSession(self, cancel)


@pytest.mark.anyio
async def test_closed_rejects_healthy_cancel_before_accept_or_enqueue(
    tmp_path: Path,
    skills_dir: Path,
) -> None:
    """healthy CLOSED 不是 frozen safe-degrade，必须保持 lifecycle 拒绝。"""
    engine, coordinator, core = await _engine_with_audit(tmp_path, skills_dir)
    result = await coordinator.finish(thread_terminals=(), reason="released")
    assert result.audit_complete
    assert coordinator.health is AuditHealth.HEALTHY
    before = await _load_journal(core)

    with pytest.raises(SessionFinishingError):
        await engine.submit(CancelTurn(submission_id="sub_target"))

    assert await _load_journal(core) == before
    assert engine._submissions.empty()  # noqa: SLF001


@pytest.mark.anyio
async def test_root_first_late_cancel_does_not_claim_cancel_turn_attribution(
    tmp_path: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session root 已先取消 target 时，late CancelTurn 不得覆盖 first winner。"""
    client = _RootFirstClient()
    engine, coordinator, core = await _engine_with_audit(
        tmp_path,
        skills_dir,
        model_client=client,
    )
    actor = asyncio.create_task(
        engine.run(CancellationToken(name="test-actor-root"))
    )
    try:
        target_id = await engine.submit(UserMessage(text="root-first"))
        with anyio.fail_after(2):
            await client.target_entered.wait()
        coordinator.session_root_cancel.cancel()
        with anyio.fail_after(2):
            await client.root_cancel_observed.wait()
        monkeypatch.setattr(
            "taifeng.loop.submission.secrets.token_hex",
            lambda _: "rootlate",
        )
        resolution_entered = anyio.Event()
        registry = coordinator._target_cancellations  # noqa: SLF001
        original_start_resolution = registry._start_resolution  # noqa: SLF001

        def observe_start_resolution(target_submission_id: str) -> Any:
            """在 first-winner 判定完成后放行测试，消除 unregister 先跑竞态。"""
            resolution = original_start_resolution(target_submission_id)
            resolution_entered.set()
            return resolution

        monkeypatch.setattr(
            registry,
            "_start_resolution",
            observe_start_resolution,
        )
        cancel_task = asyncio.create_task(
            engine.submit(CancelTurn(submission_id=target_id))
        )
        with anyio.fail_after(2):
            await resolution_entered.wait()
        client.release_target.set()
        cancel_id = await cancel_task
        records = await _wait_for_record(
            core,
            record_type="submission_applied",
            submission_id=cancel_id,
        )
        applied = SubmissionAppliedV1.model_validate(
            next(
                envelope.payload
                for envelope in records
                if envelope.submission_id == cancel_id
                and envelope.record_type == "submission_applied"
            )
        )

        assert applied.result_status == "not_found"
        assert applied.terminal_record_ids == ()
        assert not any(
            envelope.record_type == "turn_cancelled"
            and envelope.submission_id == target_id
            for envelope in records
        )
        assert coordinator.health is AuditHealth.HEALTHY
    finally:
        client.release_target.set()
        await _stop_actor(actor)
