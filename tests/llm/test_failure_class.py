"""G3：稳定的 failure_class 分类 + suggested_action + classify_failure。"""

from __future__ import annotations

import pytest

from taifeng.llm.errors import (
    AuthenticationError,
    CancelledError,
    ContextOverflowError,
    InvalidRequestError,
    LLMError,
    RateLimitError,
    ServerError,
    TransientNetworkError,
    classify_failure,
)


@pytest.mark.parametrize(
    ("exc", "expected_class"),
    [
        (ContextOverflowError("x"), "context_window"),
        (AuthenticationError("x"), "provider_auth"),
        (RateLimitError("x"), "provider_rate_limit"),
        (TransientNetworkError("x"), "provider_transport"),
        (ServerError("x"), "provider_internal"),
        (InvalidRequestError("x"), "invalid_request"),
        (CancelledError("x"), "cancelled"),
        (LLMError("x"), "unknown"),
    ],
)
def test_failure_class_mapping(exc: LLMError, expected_class: str) -> None:
    assert exc.failure_class == expected_class
    # suggested_action 非空且稳定
    assert isinstance(exc.suggested_action, str) and exc.suggested_action


def test_classify_failure_llm_error() -> None:
    cls, action = classify_failure(RateLimitError("slow down"))
    assert cls == "provider_rate_limit"
    assert action  # 非空建议


def test_classify_failure_os_error_is_runtime_io() -> None:
    cls, action = classify_failure(OSError("disk full"))
    assert cls == "runtime_io"
    assert action


def test_classify_failure_generic_is_unknown() -> None:
    cls, action = classify_failure(ValueError("boom"))
    assert cls == "unknown"
    assert action


@pytest.mark.asyncio
async def test_turn_failed_event_carries_failure_class(skills_dir) -> None:
    """LLMError 透传到 TurnRunner.run → TurnFailed 带 failure_class + 建议。"""
    from collections.abc import AsyncIterator

    from taifeng.context.budget import ContextBudget
    from taifeng.conversation.models import user_message
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.event import EventMsg
    from taifeng.loop.turn import TurnRunner
    from taifeng.skill.dispatch import DispatchPolicy
    from taifeng.skill.registry import FilesystemSkillRegistry
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.runtime import ToolCallRuntime

    # --- 一个在 stream 中抛 RateLimitError 的假 client ---
    class _RaisingSession:
        def __init__(self, cancel: CancellationToken) -> None:
            self._cancel = cancel

        async def __aenter__(self) -> _RaisingSession:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def stream(self, request: ApiRequest) -> AsyncIterator[object]:
            err = RateLimitError("429 slow down")
            err.request_id = "req-abc123"  # provider 回填的服务端 request-id
            raise err
            yield  # pragma: no cover —— 使其成为 async generator

    class _RaisingClient:
        def session(
            self, *, cancel: CancellationToken, model: str | None = None
        ) -> _RaisingSession:
            return _RaisingSession(cancel)

    registry = await FilesystemSkillRegistry.load(skills_dir)
    entry = registry.get("code-reviewer")
    assert entry is not None

    events: list = []

    async def _emit(ev: EventMsg) -> None:
        events.append(ev.msg)

    class _FakeStore:
        async def append(self, item: object) -> None:
            return None

        async def create_thread(self, **_: object) -> str:
            return "s"

    runner = TurnRunner(
        entry_skill=entry,
        snapshot=registry.snapshot(),
        model_client=_RaisingClient(),
        tool_runtime=ToolCallRuntime(ToolRegistry()),
        store=_FakeStore(),
        compressors=None,
        dispatch_policy=DispatchPolicy(),
        budget=ContextBudget(),
        thread_id="t",
        submission_id="s",
        emit=_emit,
        cancel=CancellationToken(name="t"),
        history_buffer=[user_message("hi", thread_id="t")],
    )
    await runner.run()

    failed = next(m for m in events if m.kind == "turn_failed")
    assert failed.data["failure_class"] == "provider_rate_limit"
    assert failed.data["suggested_action"]
    # G3 恢复配方也随事件透出（机读）
    assert failed.data["recovery"]["failure_class"] == "provider_rate_limit"
    assert "backoff_retry" in failed.data["recovery"]["steps"]
    # G3 request-id 回流（异常自带 → TurnFailed）
    assert failed.data["request_id"] == "req-abc123"
