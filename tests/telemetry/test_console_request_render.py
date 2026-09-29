"""console：request 留痕只打摘要、工具完成事件标出改写 / 截断（R3 渲染补齐）。"""

from __future__ import annotations

from taifeng.loop.event import EventMsg, LlmRequestRecorded, ToolCallCompleted
from taifeng.telemetry.console import _KIND_TAG, _fmt_event


def test_llm_request_recorded_renders_summary_without_body() -> None:
    data = {
        "model": "m1", "system_prompt": ["SECRET SYSTEM"],
        "messages": [{"role": "user", "content": "SECRET USER"}],
        "tools": [{"name": "t"}], "reasoning_effort": "low", "max_output_tokens": 4000,
    }
    line = _fmt_event(EventMsg(submission_id="s", msg=LlmRequestRecorded(data=data)), color=False)
    assert "llm_request_recorded" in _KIND_TAG
    assert "model=m1 system=1 input=1 tools=1 reasoning_effort=low max_output_tokens=4000" in line
    assert "SECRET" not in line


def test_tool_call_completed_flags_rewrite_and_cap() -> None:
    data = {"name": "fetch", "output": "x", "is_error": False, "duration_ms": 3,
            "output_rewritten_by_hook": True,
            "output_capped": {"original_bytes": 40909, "cap_bytes": 4096}}
    line = _fmt_event(EventMsg(submission_id="s", msg=ToolCallCompleted(data=data)), color=False)
    assert "[rewritten]" in line and "[capped 40909B]" in line
    plain = _fmt_event(EventMsg(submission_id="s", msg=ToolCallCompleted(
        data={"name": "fetch", "output": "x", "is_error": False, "duration_ms": 3})), color=False)
    assert "[rewritten]" not in plain and "[capped" not in plain
