"""Codex SSE 非协议噪声容忍契约（ADR 0030）。

背景：2026-09-05 中转网关开始往 `/responses` 流里注入 `data: {"type":"keepalive"}`
心跳帧。旧实现对未登记 type 硬失败，导致根 turn 每轮必崩、一次工具都调不出来。
本文件锁住两件事：**噪声不再阻断**，且**容忍没有外溢到协议校验**。
"""

from __future__ import annotations

import logging

import pytest

from taifeng.llm.errors import InvalidResponseError
from taifeng.llm.providers.codex.accumulator import (
    CodexResponsesAccumulator,
    NoiseLedger,
)
from taifeng.llm.providers.codex.responses import _parse_codex_sse_line
from taifeng.llm.responses_types import NormalizedMessageItem

_TEXT = "库存 A-17"
_ACC_LOGGER = "taifeng.llm.providers.codex.accumulator"


def _legal_stream() -> list[dict[str, object]]:
    """一条最小的合法 codex 流：created → added → delta → done → completed。"""
    return [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_1", "type": "message", "role": "assistant"},
        },
        {"type": "response.output_text.delta", "output_index": 0, "delta": _TEXT},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": _TEXT}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 8,
                    "total_tokens": 28,
                },
            },
        },
    ]


def _run(events: list[dict[str, object]]) -> CodexResponsesAccumulator:
    """吸收 events 并 finalize，返回 accumulator 供读噪声账。"""
    accumulator = CodexResponsesAccumulator()
    for event in events:
        accumulator.accept(event)
    accumulator.finalize()
    return accumulator


# --- 噪声不再阻断 -----------------------------------------------------------


@pytest.mark.parametrize("position", [0, 1, 2, 3, 4, 5])
def test_relay_keepalive_is_skipped_at_every_stream_position(position: int) -> None:
    """心跳落在流中任意位置（含 completed 之后）都不得终止 attempt。"""
    stream = _legal_stream()
    events = stream[:position] + [{"type": "keepalive", "ts": 1757000000}] + stream[position:]

    accumulator = CodexResponsesAccumulator()
    for event in events:
        accumulator.accept(event)
    terminal = accumulator.finalize()

    # 输出事实不受噪声影响
    assert [item.text for item in terminal.items if isinstance(item, NormalizedMessageItem)] == [
        _TEXT
    ]
    assert accumulator.noise.counts == {"event:keepalive": 1}


@pytest.mark.parametrize(
    "noise_event",
    [
        # 中转网关自造的帧
        {"type": "ping"},
        {"type": "heartbeat", "ts": 1},
        {"type": "keepalive", "comment": "relay"},
        # 上游 OpenAI Responses 里存在、但 Codex 白名单未登记的标准事件
        {"type": "response.reasoning_summary_part.added", "output_index": 0},
        {"type": "response.reasoning_summary_part.done", "output_index": 0},
        {"type": "response.output_text.annotation.added", "output_index": 0},
        {"type": "response.queued"},
        # 畸形：缺 type / type 非字符串
        {},
        {"type": 123},
    ],
)
def test_unregistered_and_malformed_frames_are_noise_not_failure(
    noise_event: dict[str, object],
) -> None:
    """未登记 type、缺 type、非字符串 type 一律记账跳过。"""
    stream = _legal_stream()
    accumulator = _run(stream[:2] + [noise_event] + stream[2:])
    assert accumulator.noise.total == 1


def test_heartbeat_flood_warns_once_per_label(caplog: pytest.LogCaptureFixture) -> None:
    """上百帧心跳只 warn 一次，但计数照实累加（不静默、也不刷屏）。"""
    stream = _legal_stream()
    flood: list[dict[str, object]] = [{"type": "keepalive"} for _ in range(120)]
    flood.extend({"type": "ping"} for _ in range(3))

    with caplog.at_level(logging.WARNING, logger=_ACC_LOGGER):
        accumulator = _run(stream[:1] + flood + stream[1:])

    assert accumulator.noise.counts == {"event:keepalive": 120, "event:ping": 3}
    assert accumulator.noise.summary() == "event:keepalivex120, event:pingx3"
    warnings = [r for r in caplog.records if r.name == _ACC_LOGGER]
    assert len(warnings) == 2  # 两个 label 各一次


# --- 容忍没有外溢到协议校验 -------------------------------------------------


@pytest.mark.parametrize(
    ("terminal_event", "match"),
    [
        ({"type": "response.failed", "response": {}}, "terminal failure"),
        ({"type": "response.incomplete", "response": {}}, "terminal failure"),
        ({"type": "error", "message": "boom"}, "terminal failure"),
    ],
)
def test_explicit_failure_events_still_fail_closed(
    terminal_event: dict[str, object], match: str
) -> None:
    """显式失败终态是协议内事件，绝不能被当成噪声吞掉。"""
    accumulator = CodexResponsesAccumulator()
    with pytest.raises(InvalidResponseError, match=match):
        accumulator.accept(terminal_event)


def test_registered_event_after_completed_still_fails() -> None:
    """completed 之后的**协议**事件仍是违规；只有噪声获豁免。"""
    accumulator = CodexResponsesAccumulator()
    for event in _legal_stream():
        accumulator.accept(event)
    accumulator.accept({"type": "keepalive"})  # 噪声：放行
    with pytest.raises(InvalidResponseError, match="after response.completed"):
        accumulator.accept({"type": "response.created", "response": {"id": "resp_2"}})


def test_noise_cannot_stand_in_for_missing_output() -> None:
    """噪声若真吞掉了内容，finalize 必然失败——终态保证未被削弱。"""
    accumulator = CodexResponsesAccumulator()
    accumulator.accept({"type": "response.created", "response": {"id": "resp_1"}})
    accumulator.accept({"type": "keepalive"})
    with pytest.raises(InvalidResponseError, match="without response.completed"):
        accumulator.finalize()


def test_identity_drift_still_fails_closed() -> None:
    """身份漂移等协议内违规不受本次放宽影响。"""
    stream = _legal_stream()
    stream[3] = {**stream[3], "item": {**stream[3]["item"], "id": "msg_other"}}  # type: ignore[dict-item]
    accumulator = CodexResponsesAccumulator()
    with pytest.raises(InvalidResponseError, match="identity changed"):
        for event in stream:
            accumulator.accept(event)


# --- 行解析层 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [": keepalive", ":", ": ping", "event: ping", "data: [DONE]", "", "id: 42"],
)
def test_transport_lines_are_skipped_without_bookkeeping(line: str) -> None:
    """SSE 注释 / 标签 / [DONE] 属正常传输语法，跳过且不记噪声账。"""
    noise = NoiseLedger()
    assert _parse_codex_sse_line(line, noise) is None
    assert noise.total == 0


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("data:", "empty-data"),
        ("data: ", "empty-data"),
        ("data: ping", "non-json-data"),
        ("data: []", "non-object-data"),
        ('data: "hello"', "non-object-data"),
    ],
)
def test_malformed_data_frames_are_recorded_and_skipped(line: str, label: str) -> None:
    """空 data / 非 JSON / 非 object 均记账跳过，不再硬失败。"""
    noise = NoiseLedger()
    assert _parse_codex_sse_line(line, noise) is None
    assert noise.counts == {label: 1}


def test_protocol_data_line_still_parses() -> None:
    """正常协议行不受影响。"""
    noise = NoiseLedger()
    parsed = _parse_codex_sse_line('data: {"type":"response.created"}', noise)
    assert parsed == {"type": "response.created"}
    assert noise.total == 0
