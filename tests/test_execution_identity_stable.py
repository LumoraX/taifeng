"""执行身份稳定契约（ADR 0143）：下游只用稳定层即可读取执行树事实。

守护三件事：``ExecutionIdentity`` 字段集合、``EventMsg.execution`` 的类型、
``execution_updated`` 承诺的数据键。字段只能增加（向后兼容），不能改名或删除。
"""

from __future__ import annotations

import asyncio
import typing
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import taifeng
from tests.conftest import GUARD_TIMEOUT_SECONDS
from tests.loop.test_usage_tree_accounting import _delegating_client

if TYPE_CHECKING:
    from pathlib import Path

_IDENTITY_FIELDS = {
    "execution_id", "attempt_id", "thread_id", "skill_id", "model",
    "parent_execution_id", "parent_thread_id",
}
_UPDATE_KEYS = {
    "phase", "iteration", "usage", "usage_complete",
    "context_tokens", "context_window", "context_measured",
    "usage_attempts_started", "usage_attempts_metered", "usage_attempts_missing",
}


def test_identity_field_set_is_frozen() -> None:
    """稳定层的字段集合与契约一致；模型不可变。"""
    assert _IDENTITY_FIELDS <= set(taifeng.ExecutionIdentity.model_fields)
    identity = taifeng.ExecutionIdentity(
        execution_id="t", attempt_id="a", thread_id="t", skill_id="s",
    )
    assert identity.parent_execution_id is None
    with pytest.raises(ValidationError):
        identity.execution_id = "other"


def test_event_envelope_references_stable_identity() -> None:
    """稳定协议字段里出现的类型必须在稳定层（public-api 规则）。"""
    hints = typing.get_type_hints(taifeng.EventMsg)
    assert taifeng.ExecutionIdentity in typing.get_args(hints["execution"])


async def test_execution_updated_carries_promised_keys(skills_dir: Path, threads_dir: Path) -> None:
    """真实 runner 发出的 execution_updated 至少包含契约承诺的键，且都带身份。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=_delegating_client(300), compressors=[],
    )
    engine = await pool.get_or_create(session_id="stable-obs", entry_skill_id="code-reviewer")
    updates: list[taifeng.EventMsg] = []
    done = asyncio.Event()

    async def collect() -> None:
        """收集到根执行结束。"""
        async for event in engine.subscribe_all():
            if event.msg.kind == "execution_updated":
                updates.append(event)
            if (event.msg.kind == "turn_completed"
                    and event.msg.data.get("thread_id") == engine.thread_id):
                done.set()
                return

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    try:
        await engine.submit(taifeng.UserMessage(text="审查代码"))
        await asyncio.wait_for(done.wait(), GUARD_TIMEOUT_SECONDS)
        assert updates
        for event in updates:
            assert isinstance(event.execution, taifeng.ExecutionIdentity)
            assert _UPDATE_KEYS <= set(event.msg.data)
            data = event.msg.data
            assert data["usage_attempts_missing"] == (
                data["usage_attempts_started"] - data["usage_attempts_metered"]
            )
    finally:
        task.cancel()
        await pool.close()
