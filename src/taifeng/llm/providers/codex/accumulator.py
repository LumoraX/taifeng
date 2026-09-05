"""Codex done-item SSE 的 fail-closed terminal accumulator。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from taifeng.llm.errors import ContentFilterError, InvalidResponseError
from taifeng.llm.events import (
    ResponseEvent,
    reasoning_delta,
    text_delta,
    tool_call_delta,
    tool_call_done,
)
from taifeng.llm.providers._shared import extract_usage_openai_family
from taifeng.llm.responses_types import (
    NormalizedFunctionCallItem,
    NormalizedMessageItem,
    NormalizedOutputItem,
    NormalizedReasoningItem,
    NormalizedRefusalItem,
)
from taifeng.llm.types import ProviderStateEnvelope, TokenUsage

logger = logging.getLogger(__name__)

_OUTPUT_TYPES = frozenset({"reasoning", "message", "function_call"})
_PART_TYPES = frozenset({"output_text", "refusal"})

# 带正文的 delta/done 事件（统一走 _accept_value_event）
_VALUE_EVENTS = frozenset(
    {
        "response.output_text.delta",
        "response.output_text.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.refusal.delta",
        "response.refusal.done",
    }
)

# Codex Responses 协议已登记的顶层 SSE ``type`` 全集。**不在此集合内的一律不是
# 协议事件**——见 NoiseLedger 与 accept() 的容忍规则（ADR 0030）。
_KNOWN_EVENTS = (
    frozenset(
        {
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
            "response.failed",
            "response.incomplete",
            "error",
        }
    )
    | _VALUE_EVENTS
)


@dataclass(slots=True)
class NoiseLedger:
    """一次 attempt 内被跳过的**非协议帧**记账（ADR 0030）。

    中转网关会往 SSE 流里注入自己的帧——心跳 / 计费标记 / 路由探针——它们不属于
    Codex Responses 协议。跳过而非硬失败，但**不静默**：同一 label 每个 attempt
    warn 一次（心跳可能上百帧，不能刷屏），计数留给调用方与测试读。

    Attributes:
        counts: label → 出现次数。label 形如 ``event:keepalive``（未登记 type）
            或 ``empty-data`` / ``non-json-data`` / ``non-object-data``（行解析层）。
    """

    counts: dict[str, int] = field(default_factory=dict)

    def record(self, label: str) -> None:
        """记一笔非协议帧；该 label 首次出现时 warn 一次。"""
        first = label not in self.counts
        self.counts[label] = self.counts.get(label, 0) + 1
        if first:
            logger.warning(
                "Codex SSE 跳过非协议帧（疑似中转网关注入）：%s；"
                "终态保证不受影响——输出事实仍由 done items + completed 完成门把关",
                label,
            )

    @property
    def total(self) -> int:
        """本次 attempt 跳过的非协议帧总数。"""
        return sum(self.counts.values())

    def summary(self) -> str:
        """稳定排序的 ``label×次数`` 摘要（日志 / 断言用）。"""
        return ", ".join(f"{label}x{count}" for label, count in sorted(self.counts.items()))


@dataclass(slots=True)
class _PartSlot:
    """单个 message content part 的配对状态。"""

    kind: str
    deltas: list[str] = field(default_factory=list)
    done: bool = False
    terminal: str | None = None


@dataclass(slots=True)
class _OutputSlot:
    """单个 output_index 的 added/delta/done 状态。"""

    index: int
    kind: str
    item_id: str
    added: dict[str, Any]
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)
    refusal: list[str] = field(default_factory=list)
    seen_text: bool = False
    seen_reasoning: bool = False
    seen_arguments: bool = False
    seen_refusal: bool = False
    parts: list[_PartSlot] = field(default_factory=list)
    done_raw: dict[str, Any] | None = None
    normalized: NormalizedOutputItem | None = None


@dataclass(frozen=True, slots=True)
class CodexTerminal:
    """clean EOF 后可发布的唯一 terminal 结果。"""

    response_id: str
    items: tuple[NormalizedOutputItem, ...]
    usage: TokenUsage


def _strict_index(event: dict[str, Any], key: str) -> int:
    """拒绝 bool、负数和缺失的 SSE index。"""
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidResponseError(f"Codex {key} must be a non-negative integer")
    return value


def _non_empty(value: object, label: str) -> str:
    """读取稳定非空字符串身份。"""
    if not isinstance(value, str) or not value:
        raise InvalidResponseError(f"Codex {label} must be non-empty")
    return value


def _part_value(part: dict[str, Any]) -> tuple[str, str]:
    """白名单读取 terminal content part。"""
    kind = part.get("type")
    if kind not in _PART_TYPES:
        raise InvalidResponseError(f"unsupported Codex content part: {kind}")
    key = "text" if kind == "output_text" else "refusal"
    value = part.get(key)
    if not isinstance(value, str):
        raise InvalidResponseError(f"Codex {kind} terminal value must be a string")
    return str(kind), value


def _canonical_part(part: dict[str, Any]) -> dict[str, Any]:
    """投影 done/completed 比较所需的 content 白名单字段。"""
    kind, value = _part_value(part)
    key = "text" if kind == "output_text" else "refusal"
    return {"type": kind, key: value}


def _canonical_item(raw: dict[str, Any]) -> dict[str, Any]:
    """投影 done/completed 冲突比较的 item 白名单。"""
    kind = raw.get("type")
    if kind == "message":
        content = raw.get("content")
        if not isinstance(content, list):
            raise InvalidResponseError("Codex message content must be a list")
        return {
            key: value
            for key, value in {
                "id": raw.get("id"),
                "type": kind,
                "role": raw.get("role"),
                "status": raw.get("status"),
                "content": [
                    _canonical_part(part)
                    for part in content
                    if isinstance(part, dict)
                ],
            }.items()
            if value is not None
        }
    keys = (
        ("id", "type", "encrypted_content", "summary", "status")
        if kind == "reasoning"
        else ("id", "type", "call_id", "name", "arguments", "status")
    )
    return {key: raw[key] for key in keys if key in raw}


def _strict_usage(raw: object) -> TokenUsage:
    """验证 completed usage，不允许 bool/int coercion。"""
    if not isinstance(raw, dict):
        raise InvalidResponseError("Codex completed usage must be an object")
    counts: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidResponseError(f"Codex usage {key} must be a non-negative integer")
        counts[key] = value
    if counts["total_tokens"] != counts["input_tokens"] + counts["output_tokens"]:
        raise InvalidResponseError("Codex usage total_tokens is inconsistent")
    for key in ("input_tokens_details", "output_tokens_details"):
        details = raw.get(key)
        if details is None:
            continue
        if not isinstance(details, dict):
            raise InvalidResponseError(f"Codex usage {key} must be an object")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in details.values()
        ):
            raise InvalidResponseError(f"Codex usage {key} contains invalid counts")
    return extract_usage_openai_family(raw)


class CodexResponsesAccumulator:
    """单次 Codex attempt 的 SSE 配对与 terminal 一致性状态机。"""

    def __init__(self, noise: NoiseLedger | None = None) -> None:
        self._slots: list[_OutputSlot] = []
        self._completed: dict[str, Any] | None = None
        # 行解析层与本状态机共用一本噪声账（调用方可注入以跨两层聚合）
        self.noise = noise if noise is not None else NoiseLedger()

    def accept(self, event: dict[str, Any]) -> list[ResponseEvent]:
        """吸收一个解析成功的非空 SSE data object。

        **未登记的顶层 type 一律跳过，不再硬失败**（ADR 0030）：中转网关会往流里
        注入自己的帧（心跳 / 计费 / 路由标记），据此终止整条流等于把第三方链路噪声
        升格成不可恢复故障——2026-09-05 中转站开始发 ``{"type":"keepalive"}`` 后
        根 turn 每轮必崩即是此故障。跳过不削弱终态保证：输出事实仍由 done items
        与 completed 完成门把关，噪声若真吞掉了内容，``finalize()`` 必然失败。

        仍然硬失败的是**协议内**的违规：显式失败终态、身份漂移、配对缺失、
        delta 与 done 不一致、completed 后又来协议事件。
        """
        kind = event.get("type")
        # 非协议帧（未登记 type / 缺 type）：记账后跳过。该判定必须在 completed
        # 闸门之前——心跳同样会落在 completed 与 EOF 之间的空窗里。
        if not isinstance(kind, str) or kind not in _KNOWN_EVENTS:
            self.noise.record(f"event:{kind}")
            return []
        if self._completed is not None:
            raise InvalidResponseError("Codex emitted event after response.completed")
        if kind in {"response.created", "response.in_progress"}:
            return []
        if kind == "response.output_item.added":
            self._accept_output_added(event)
            return []
        if kind == "response.content_part.added":
            self._accept_part_added(event)
            return []
        if kind == "response.content_part.done":
            self._accept_part_done(event)
            return []
        if kind == "response.output_item.done":
            return self._accept_output_done(event)
        if kind == "response.completed":
            self._accept_completed(event)
            return []
        if kind in {"response.failed", "response.incomplete", "error"}:
            raise InvalidResponseError(f"Codex terminal failure: {kind}")
        # _KNOWN_EVENTS 已穷举，剩余必为 _VALUE_EVENTS
        return self._accept_value_event(event, kind)

    def _accept_output_added(self, event: dict[str, Any]) -> None:
        """登记连续 output index 与稳定 item identity。"""
        index = _strict_index(event, "output_index")
        if index != len(self._slots):
            raise InvalidResponseError("Codex output indexes must be continuous")
        item = event.get("item")
        if not isinstance(item, dict):
            raise InvalidResponseError("Codex output item added must contain an object")
        kind = item.get("type")
        if kind not in _OUTPUT_TYPES:
            raise InvalidResponseError(f"unsupported Codex output item: {kind}")
        item_id = _non_empty(item.get("id"), "output item id")
        if kind == "function_call":
            _non_empty(item.get("call_id"), "function call id")
            _non_empty(item.get("name"), "function call name")
        self._slots.append(
            _OutputSlot(index=index, kind=str(kind), item_id=item_id, added=dict(item))
        )

    def _slot(self, event: dict[str, Any]) -> _OutputSlot:
        """解析 output_index，并拒绝 done 后的新事件。"""
        index = _strict_index(event, "output_index")
        if index >= len(self._slots):
            raise InvalidResponseError("Codex event references unknown output index")
        slot = self._slots[index]
        if slot.done_raw is not None:
            raise InvalidResponseError("Codex emitted event after output item done")
        return slot

    def _accept_part_added(self, event: dict[str, Any]) -> None:
        """登记连续 message content index。"""
        slot = self._slot(event)
        if slot.kind != "message":
            raise InvalidResponseError("Codex content part belongs to non-message item")
        index = _strict_index(event, "content_index")
        if index != len(slot.parts):
            raise InvalidResponseError("Codex content indexes must be continuous")
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") not in _PART_TYPES:
            raise InvalidResponseError("unsupported Codex content part added")
        slot.parts.append(_PartSlot(kind=str(part["type"])))

    def _part(self, slot: _OutputSlot, event: dict[str, Any]) -> _PartSlot | None:
        """有 content_index 时要求对应 added part；省略时允许 direct delta。"""
        if "content_index" not in event:
            return None
        index = _strict_index(event, "content_index")
        if index >= len(slot.parts):
            raise InvalidResponseError("Codex event references unknown content index")
        part = slot.parts[index]
        if part.done:
            raise InvalidResponseError("Codex emitted event after content part done")
        return part

    def _accept_part_done(self, event: dict[str, Any]) -> None:
        """完成 content part，并逐字节核对已见 delta。"""
        slot = self._slot(event)
        part_slot = self._part(slot, event)
        if part_slot is None:
            raise InvalidResponseError("Codex content part done is missing content index")
        raw = event.get("part")
        if not isinstance(raw, dict):
            raise InvalidResponseError("Codex content part done must contain part")
        kind, value = _part_value(raw)
        if kind != part_slot.kind:
            raise InvalidResponseError("Codex content part identity changed")
        if part_slot.deltas and "".join(part_slot.deltas) != value:
            raise InvalidResponseError("Codex content delta does not match terminal part")
        part_slot.done = True
        part_slot.terminal = value

    def _accept_value_event(
        self,
        event: dict[str, Any],
        kind: str,
    ) -> list[ResponseEvent]:
        """吸收 text/reasoning/arguments/refusal 的 delta 或 done。"""
        slot = self._slot(event)
        if kind.startswith("response.output_text"):
            return self._text_event(slot, event, kind)
        if kind.startswith("response.reasoning_summary_text"):
            return self._reasoning_event(slot, event, kind)
        if kind.startswith("response.function_call_arguments"):
            return self._arguments_event(slot, event, kind)
        return self._refusal_event(slot, event, kind)

    def _text_event(
        self, slot: _OutputSlot, event: dict[str, Any], kind: str
    ) -> list[ResponseEvent]:
        """校验 message text event。"""
        if slot.kind != "message":
            raise InvalidResponseError("Codex text event belongs to non-message item")
        part = self._part(slot, event)
        if part is not None and part.kind != "output_text":
            raise InvalidResponseError("Codex text event content identity changed")
        if kind.endswith(".delta"):
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise InvalidResponseError("Codex text delta must be a string")
            slot.seen_text = True
            slot.text.append(delta)
            if part is not None:
                part.deltas.append(delta)
            return [text_delta(delta)] if delta else []
        self._match_done(slot.text, slot.seen_text, event.get("text"), "text")
        return []

    def _reasoning_event(
        self, slot: _OutputSlot, event: dict[str, Any], kind: str
    ) -> list[ResponseEvent]:
        """校验 reasoning summary event。"""
        if slot.kind != "reasoning":
            raise InvalidResponseError("Codex reasoning event belongs to wrong item")
        if kind.endswith(".delta"):
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise InvalidResponseError("Codex reasoning delta must be a string")
            slot.seen_reasoning = True
            slot.reasoning.append(delta)
            return [reasoning_delta(delta)] if delta else []
        self._match_done(
            slot.reasoning,
            slot.seen_reasoning,
            event.get("text"),
            "reasoning",
        )
        return []

    def _arguments_event(
        self, slot: _OutputSlot, event: dict[str, Any], kind: str
    ) -> list[ResponseEvent]:
        """校验 function arguments event。"""
        if slot.kind != "function_call":
            raise InvalidResponseError("Codex arguments event belongs to wrong item")
        if kind.endswith(".delta"):
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise InvalidResponseError("Codex arguments delta must be a string")
            slot.seen_arguments = True
            slot.arguments.append(delta)
            return [
                tool_call_delta(
                    call_id=str(slot.added["call_id"]),
                    name=str(slot.added["name"]),
                    delta=delta,
                )
            ] if delta else []
        self._match_done(
            slot.arguments,
            slot.seen_arguments,
            event.get("arguments"),
            "function arguments",
        )
        return []

    def _refusal_event(
        self, slot: _OutputSlot, event: dict[str, Any], kind: str
    ) -> list[ResponseEvent]:
        """缓冲 refusal，不把正文作为普通 assistant preview 发布。"""
        if slot.kind != "message":
            raise InvalidResponseError("Codex refusal belongs to non-message item")
        part = self._part(slot, event)
        if part is not None and part.kind != "refusal":
            raise InvalidResponseError("Codex refusal content identity changed")
        if kind.endswith(".delta"):
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise InvalidResponseError("Codex refusal delta must be a string")
            slot.seen_refusal = True
            slot.refusal.append(delta)
            if part is not None:
                part.deltas.append(delta)
            return []
        self._match_done(
            slot.refusal,
            slot.seen_refusal,
            event.get("refusal"),
            "refusal",
        )
        return []

    @staticmethod
    def _match_done(
        deltas: list[str], seen: bool, terminal: object, label: str
    ) -> None:
        """done value 必须是字符串，且与已见 delta 逐字节一致。"""
        if not isinstance(terminal, str):
            raise InvalidResponseError(f"Codex {label} done must be a string")
        if seen and "".join(deltas) != terminal:
            raise InvalidResponseError(f"Codex {label} delta does not match done")

    def _accept_output_done(self, event: dict[str, Any]) -> list[ResponseEvent]:
        """校验 done identity/body，并保存唯一 normalized fact。"""
        slot = self._slot(event)
        raw = event.get("item")
        if not isinstance(raw, dict):
            raise InvalidResponseError("Codex output item done must contain an object")
        if raw.get("type") != slot.kind or raw.get("id") != slot.item_id:
            raise InvalidResponseError("Codex output item identity changed")
        if slot.kind == "function_call" and (
            raw.get("call_id") != slot.added.get("call_id")
            or raw.get("name") != slot.added.get("name")
        ):
            raise InvalidResponseError("Codex function call identity changed")
        normalized = self._normalize_done(slot, raw)
        slot.done_raw = dict(raw)
        slot.normalized = normalized
        if isinstance(normalized, NormalizedRefusalItem):
            if not normalized.text:
                raise InvalidResponseError("Codex emitted empty refusal")
            raise ContentFilterError(normalized.text)
        if isinstance(normalized, NormalizedFunctionCallItem):
            return [
                tool_call_done(
                    normalized.call_id,
                    normalized.name,
                    normalized.arguments,
                )
            ]
        return []

    def _normalize_done(
        self, slot: _OutputSlot, raw: dict[str, Any]
    ) -> NormalizedOutputItem:
        """把已验证 done item 投影为 provider-neutral terminal item。"""
        if slot.kind == "message":
            return self._normalize_message(slot, raw)
        if slot.kind == "reasoning":
            return self._normalize_reasoning(slot, raw)
        arguments = raw.get("arguments")
        if not isinstance(arguments, str):
            raise InvalidResponseError("Codex function arguments must be a string")
        if slot.seen_arguments and "".join(slot.arguments) != arguments:
            raise InvalidResponseError(
                "Codex function arguments delta does not match terminal item"
            )
        return NormalizedFunctionCallItem(
            output_index=slot.index,
            call_id=_non_empty(raw.get("call_id"), "function call id"),
            name=_non_empty(raw.get("name"), "function call name"),
            arguments=arguments,
        )

    def _normalize_message(
        self, slot: _OutputSlot, raw: dict[str, Any]
    ) -> NormalizedOutputItem:
        """校验 message parts、delta 和 refusal 互斥。"""
        content = raw.get("content")
        if not isinstance(content, list) or not all(
            isinstance(part, dict) for part in content
        ):
            raise InvalidResponseError("Codex message content must contain objects")
        if slot.parts:
            if len(content) != len(slot.parts) or any(not part.done for part in slot.parts):
                raise InvalidResponseError("Codex content parts are incomplete")
            for raw_part, tracked in zip(content, slot.parts, strict=True):
                kind, value = _part_value(raw_part)
                if kind != tracked.kind or value != tracked.terminal:
                    raise InvalidResponseError("Codex terminal content part changed")
        texts: list[str] = []
        refusals: list[str] = []
        for part in content:
            kind, value = _part_value(part)
            (texts if kind == "output_text" else refusals).append(value)
        if texts and refusals:
            raise InvalidResponseError("Codex refusal cannot mix with output text")
        if refusals:
            refusal = "".join(refusals)
            if slot.seen_refusal and "".join(slot.refusal) != refusal:
                raise InvalidResponseError("Codex refusal delta does not match terminal item")
            return NormalizedRefusalItem(output_index=slot.index, text=refusal)
        text = "".join(texts)
        if slot.seen_text and "".join(slot.text) != text:
            raise InvalidResponseError("Codex text delta does not match terminal item")
        return NormalizedMessageItem(output_index=slot.index, text=text)

    def _normalize_reasoning(
        self, slot: _OutputSlot, raw: dict[str, Any]
    ) -> NormalizedReasoningItem:
        """投影 reasoning summary 与可选 encrypted state。"""
        summary = raw.get("summary", [])
        if not isinstance(summary, list) or not all(
            isinstance(part, dict) for part in summary
        ):
            raise InvalidResponseError("Codex reasoning summary must be a list")
        visible = "".join(str(part.get("text", "")) for part in summary)
        if slot.seen_reasoning and "".join(slot.reasoning) != visible:
            raise InvalidResponseError("Codex reasoning delta does not match terminal item")
        state = None
        encrypted = raw.get("encrypted_content")
        if encrypted is not None:
            _non_empty(encrypted, "reasoning encrypted_content")
            payload = {
                key: raw[key]
                for key in ("id", "type", "encrypted_content", "summary", "status")
                if key in raw
            }
            state = ProviderStateEnvelope(
                provider="codex",
                protocol="responses",
                item_type="reasoning",
                payload=payload,
            )
        return NormalizedReasoningItem(
            output_index=slot.index,
            visible_text=visible,
            state=state,
        )

    def _accept_completed(self, event: dict[str, Any]) -> None:
        """登记唯一 completion gate；terminal 发布延迟到 clean EOF。"""
        if not self._slots or any(slot.done_raw is None for slot in self._slots):
            raise InvalidResponseError("Codex completed requires at least one done item")
        response = event.get("response")
        if not isinstance(response, dict):
            raise InvalidResponseError("Codex response.completed is missing response")
        self._completed = response

    def finalize(self) -> CodexTerminal:
        """在 SSE clean EOF 后校验 completed，并返回可发布 terminal。"""
        response = self._completed
        if response is None:
            raise InvalidResponseError("Codex stream ended without response.completed")
        response_id = _non_empty(response.get("id"), "completed response id")
        if response.get("status") != "completed":
            raise InvalidResponseError("Codex completed status must be completed")
        usage = _strict_usage(response.get("usage"))
        output = response.get("output")
        if not isinstance(output, list):
            raise InvalidResponseError("Codex completed output must be a list")
        if output:
            self._match_completed_output(output)
        items = tuple(slot.normalized for slot in self._slots)
        if any(item is None for item in items):
            raise InvalidResponseError("Codex normalized done item is missing")
        return CodexTerminal(
            response_id=response_id,
            items=items,  # type: ignore[arg-type]
            usage=usage,
        )

    def _match_completed_output(self, output: list[Any]) -> None:
        """非空 completed.output 必须与 done items canonical 等价。"""
        if len(output) != len(self._slots):
            raise InvalidResponseError("Codex completed output conflicts with done items")
        for position, raw in enumerate(output):
            if not isinstance(raw, dict):
                raise InvalidResponseError("Codex completed output item must be an object")
            if "output_index" in raw:
                index = raw["output_index"]
                if isinstance(index, bool) or not isinstance(index, int) or index != position:
                    raise InvalidResponseError("Codex completed output index conflicts")
            done = self._slots[position].done_raw
            assert done is not None
            if _canonical_item(raw) != _canonical_item(done):
                raise InvalidResponseError("Codex completed output conflicts with done items")


__all__ = ["CodexResponsesAccumulator", "CodexTerminal", "NoiseLedger"]
