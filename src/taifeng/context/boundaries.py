"""Responses/legacy LLM sample 的连续压缩闭包解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.llm.errors import InvalidHistoryError

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem


@dataclass(frozen=True, slots=True)
class SampleSpan:
    """同一 logical sample 全部成员形成的最小连续跨度。"""

    sample_id: str
    start: int
    end: int


def _nonempty_metadata_id(item: ResponseItem, key: str) -> str | None:
    """读取显式 sample id，并拒绝畸形保留 metadata。"""
    value = item.metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidHistoryError(f"{key} must be a non-empty string")
    return value


def _collect_explicit_memberships(
    history: list[ResponseItem],
) -> tuple[dict[int, str], dict[str, int]]:
    """收集新记录的 sample metadata，并校验 call id 唯一性。"""
    memberships: dict[int, str] = {}
    call_indices: dict[str, int] = {}
    output_indices: dict[str, set[int]] = {}
    for index, item in enumerate(history):
        sample_id = _nonempty_metadata_id(item, "llm_sample_id")
        origin_id = _nonempty_metadata_id(item, "origin_llm_sample_id")
        if sample_id is not None and origin_id is not None:
            raise InvalidHistoryError("item cannot declare both sample and origin sample")
        owner = sample_id or origin_id
        if owner is not None:
            memberships[index] = owner
        provider_index = item.metadata.get("provider_output_index")
        if provider_index is not None:
            if (
                isinstance(provider_index, bool)
                or not isinstance(provider_index, int)
                or provider_index < 0
                or sample_id is None
            ):
                raise InvalidHistoryError("invalid provider output index metadata")
            seen = output_indices.setdefault(sample_id, set())
            if provider_index in seen:
                raise InvalidHistoryError("duplicate provider output index in sample")
            seen.add(provider_index)
        if item.kind == "function_call":
            call_id = item.payload.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise InvalidHistoryError("function call requires call_id")
            if call_id in call_indices:
                raise InvalidHistoryError(f"duplicate function call id: {call_id}")
            call_indices[call_id] = index
    return memberships, call_indices


def _assign_legacy_memberships(
    history: list[ResponseItem],
    memberships: dict[int, str],
    call_indices: dict[str, int],
) -> None:
    """为旧记录按确定性窗口算法补临时 sample id。"""
    pending_reasoning: list[int] = []
    current: str | None = None
    legacy_ordinal = 0
    for index, item in enumerate(history):
        explicit = memberships.get(index)
        if explicit is not None:
            if pending_reasoning:
                raise InvalidHistoryError("orphan legacy reasoning before explicit sample")
            if item.kind not in {"function_call_output"}:
                current = None
            continue
        if item.kind == "reasoning":
            if pending_reasoning:
                raise InvalidHistoryError("consecutive legacy reasoning is ambiguous")
            current = None
            pending_reasoning = [index]
        elif item.kind == "assistant_message":
            current = f"legacy:{legacy_ordinal}"
            legacy_ordinal += 1
            for member in (*pending_reasoning, index):
                memberships[member] = current
            pending_reasoning = []
        elif item.kind == "function_call":
            if current is None:
                current = f"legacy:{legacy_ordinal}"
                legacy_ordinal += 1
                for member in pending_reasoning:
                    memberships[member] = current
                pending_reasoning = []
            memberships[index] = current
        elif item.kind == "function_call_output":
            call_id = item.payload.get("call_id")
            call_index = call_indices.get(str(call_id))
            owner = memberships.get(call_index) if call_index is not None else None
            if owner is None:
                raise InvalidHistoryError(f"orphan function call output: {call_id}")
            memberships[index] = owner
        elif item.kind in {"user_message", "system_injection", "compacted"}:
            if pending_reasoning:
                raise InvalidHistoryError("legacy reasoning has no terminal output")
            current = None
    if pending_reasoning:
        raise InvalidHistoryError("legacy reasoning has no terminal output")


def sample_spans(history: list[ResponseItem]) -> tuple[SampleSpan, ...]:
    """解析全部显式/legacy sample 的最小连续跨度。"""
    memberships, call_indices = _collect_explicit_memberships(history)
    _assign_legacy_memberships(history, memberships, call_indices)
    for index, item in enumerate(history):
        if item.kind != "function_call_output":
            continue
        call_id = str(item.payload.get("call_id", ""))
        call_index = call_indices.get(call_id)
        if call_index is None:
            raise InvalidHistoryError(f"orphan function call output: {call_id}")
        call_owner = memberships.get(call_index)
        output_owner = memberships.get(index)
        if call_owner is None or output_owner is None or call_owner != output_owner:
            raise InvalidHistoryError("function output sample does not match its call")
    members_by_sample: dict[str, list[int]] = {}
    for index, sample_id in memberships.items():
        members_by_sample.setdefault(sample_id, []).append(index)
    return tuple(
        SampleSpan(sample_id=sample_id, start=min(indices), end=max(indices) + 1)
        for sample_id, indices in members_by_sample.items()
    )


def resolve_compaction_range(
    history: list[ResponseItem],
    start: int,
    end: int,
    *,
    protected_before: int | None = None,
    protected_from: int | None = None,
) -> tuple[int, int]:
    """把候选区间收敛为不切断 sample 的连续删除区间。"""
    if not 0 <= start <= end <= len(history):
        raise ValueError("compaction range is outside history")
    left_guard = start if protected_before is None else protected_before
    right_guard = end if protected_from is None else protected_from
    if not 0 <= left_guard <= start or not end <= right_guard <= len(history):
        raise ValueError("protected ranges do not contain the candidate")
    spans = sample_spans(history)
    changed = True
    while changed and start < end:
        changed = False
        for span in spans:
            if span.start >= end or span.end <= start:
                continue
            if span.start < start:
                start = max(start, span.end) if span.start < left_guard else span.start
                changed = True
                break
            if span.end > end:
                end = min(end, span.start) if span.end > right_guard else span.end
                changed = True
                break
    return min(start, end), end
