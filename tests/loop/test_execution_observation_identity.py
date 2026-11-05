"""执行身份的数据契约与旧 EventMsg 兼容性。"""

from __future__ import annotations

import importlib
import importlib.util

import pytest
from pydantic import ValidationError

from taifeng.loop.event import AssistantText, EventMsg


def _identity():
    """缺少身份模块时给出可定位的红测试。"""
    name = 'taifeng.loop.execution_identity'
    assert importlib.util.find_spec(name) is not None, '缺少执行身份协议'
    return importlib.import_module(name).ExecutionIdentity(
        execution_id='thread', attempt_id='attempt', thread_id='thread', skill_id='worker',
    )


def test_identity_is_frozen() -> None:
    """观察字段不能被订阅者修改后影响其他消费者。"""
    identity = _identity()
    assert identity.parent_execution_id is None
    with pytest.raises(ValidationError):
        identity.skill_id = 'changed'


def test_old_event_defaults_to_no_execution() -> None:
    """旧 JSON 事件保持可读，缺少身份明确为 None。"""
    event = EventMsg(submission_id='s', msg=AssistantText(data={'delta': 'hi'}))
    assert event.execution is None


def test_execution_identity_survives_json_roundtrip() -> None:
    """身份作为元数据随 JSONL/SSE 往返，不靠解析 delta。"""
    event = EventMsg(submission_id='s', msg=AssistantText(data={'delta': 'hi'}), execution=_identity())
    restored = EventMsg.model_validate_json(event.model_dump_json())
    assert restored.execution == event.execution
