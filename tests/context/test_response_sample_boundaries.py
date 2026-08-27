"""Responses sample 原子压缩边界与密文脱敏视图测试。"""

from __future__ import annotations

import pytest

from taifeng.context.boundaries import resolve_compaction_range
from taifeng.context.compaction_view import CompactionView
from taifeng.context.strategies.handoff import HandoffCompactionStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    reasoning,
    suspension_item,
    user_message,
)
from taifeng.llm.errors import InvalidHistoryError
from taifeng.llm.providers import SimClient, SimTurn


def _sample_item(item: ResponseItem, sample_id: str, output_index: int) -> ResponseItem:
    """给 terminal output item 注入显式 sample 顺序元数据。"""
    return item.model_copy(
        update={
            "metadata": {
                **item.metadata,
                "llm_sample_id": sample_id,
                "provider_output_index": output_index,
            }
        }
    )


def _output(item: ResponseItem, sample_id: str) -> ResponseItem:
    """给 tool output 注入来源 sample 元数据。"""
    return item.model_copy(
        update={"metadata": {**item.metadata, "origin_llm_sample_id": sample_id}}
    )


def _explicit_history() -> list[ResponseItem]:
    """构造 reasoning→assistant→parallel calls→交错 outputs 的一轮历史。"""
    sample = "sample-1"
    return [
        user_message("看图", thread_id="t"),
        _sample_item(reasoning("", summary="检查", thread_id="t"), sample, 0),
        _sample_item(assistant_message("", thread_id="t", model="gpt-5.6"), sample, 1),
        _sample_item(function_call("c1", "one", "{}", thread_id="t"), sample, 2),
        _sample_item(function_call("c2", "two", "{}", thread_id="t"), sample, 3),
        suspension_item(
            record_id="susp-1",
            submission_id="sub-1",
            turn_index=0,
            pending=[],
            created_at=1,
            thread_id="t",
        ),
        _output(function_call_output("c2", "two-ok", thread_id="t"), sample),
        _output(function_call_output("c1", "one-ok", thread_id="t"), sample),
        user_message("继续", thread_id="t"),
    ]


def test_protected_tail_shrinks_before_entire_explicit_sample() -> None:
    """候选切进 sample 且其 output 在保护尾部时，必须保留整组。"""
    history = _explicit_history()

    assert resolve_compaction_range(history, 0, 6) == (0, 1)


def test_candidate_containing_sample_expands_through_interleaved_outputs() -> None:
    """无保护冲突时，closure 扩展到同 sample 的最后一个 output。"""
    history = _explicit_history()

    assert resolve_compaction_range(history, 1, 5, protected_from=len(history)) == (1, 8)


def test_duplicate_call_id_fails_closed() -> None:
    """重复 call id 无法唯一归属时不得猜测 compaction 边界。"""
    history = [
        function_call("dup", "one", "{}", thread_id="t"),
        function_call("dup", "two", "{}", thread_id="t"),
    ]

    with pytest.raises(InvalidHistoryError):
        resolve_compaction_range(history, 0, 1)


def test_compaction_view_removes_encrypted_state_and_reserved_metadata() -> None:
    """摘要视图只保留可见 reasoning，不携带 provider state 或 sample metadata。"""
    item = _sample_item(
        ResponseItem(
            kind="reasoning",
            thread_id="t",
            payload={
                "text": "可见推理",
                "summary": "可见摘要",
                "provider_state": {
                    "provider": "openai",
                    "protocol": "responses",
                    "item_type": "reasoning",
                    "payload": {"encrypted_content": "SENTINEL-CIPHERTEXT"},
                },
            },
        ),
        "sample-1",
        0,
    )

    formatted = CompactionView.from_items([item]).format_for_summary()

    assert "可见推理" in formatted
    assert "可见摘要" in formatted
    assert "SENTINEL-CIPHERTEXT" not in formatted
    assert "llm_sample_id" not in formatted


@pytest.mark.asyncio
async def test_handoff_request_never_contains_encrypted_provider_state() -> None:
    """真实 handoff request 构造只能消费脱敏 CompactionView。"""
    client = SimClient(turns=[SimTurn(text="## 进度\n- 已完成")])
    strategy = HandoffCompactionStrategy(model_client=client, model="sim")
    item = ResponseItem(
        kind="reasoning",
        thread_id="t",
        payload={
            "text": "visible",
            "summary": "summary",
            "provider_state": {"payload": {"encrypted_content": "SENTINEL-CIPHERTEXT"}},
        },
    )

    text, error = await strategy._generate_summary([item], 0, 1)  # noqa: SLF001

    assert error is None
    assert text
    request = client.ledger.single_request().request
    assert "SENTINEL-CIPHERTEXT" not in request.messages[-1].content
