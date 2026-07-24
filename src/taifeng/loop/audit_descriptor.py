"""Audited submission 自由输入的 bounded、无 hook descriptor。"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING, cast

from taifeng.conversation.journal.canonical import canonical_hash
from taifeng.loop.submission import Submission, UserMessage

if TYPE_CHECKING:
    from taifeng.conversation.journal.models import JsonValue

_MAX_DEPTH = 12
_MAX_NODES = 64
_MAX_CONTAINER_ENTRIES = 16
_MAX_STRING_CHARS = 128
_MAX_KEY_CHARS = 64
_MAX_SAFE_INTEGER = (1 << 53) - 1
_HASH_PREFIX_CHARS = 64
_UNHANDLED = object()


class _WalkState:
    """持有一次 descriptor walk 的全局预算与 active 容器集合。"""

    __slots__ = ("active", "remaining")

    def __init__(self) -> None:
        """初始化固定 node budget。"""
        self.active: set[int] = set()
        self.remaining = _MAX_NODES

    def consume(self) -> bool:
        """消费一个 node；预算耗尽时返回 False。"""
        if self.remaining == 0:
            return False
        self.remaining -= 1
        return True


def _marker(reason: str, **facts: JsonValue) -> dict[str, JsonValue]:
    """构造只含稳定 bounded facts 的 marker。"""
    return {"invalid": reason, **facts}


def _bounded_count(value: int) -> int:
    """把长度/bit length 限制到 RFC 8785 安全整数域。"""
    return min(value, _MAX_SAFE_INTEGER)


def _prefix_hash(value: str) -> str:
    """只散列 bounded 前缀，并安全覆盖 lone surrogate。"""
    prefix = value[:_HASH_PREFIX_CHARS].encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(prefix).hexdigest()


def _safe_string(
    value: str,
    *,
    limit: int,
    too_long: str,
) -> JsonValue:
    """保留短合法 Unicode；其余只返回长度与 bounded 前缀 hash。"""
    length = len(value)
    if length > limit:
        return _marker(
            too_long,
            length=_bounded_count(length),
            prefix_hash=_prefix_hash(value),
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return _marker(
            "invalid_unicode",
            length=_bounded_count(length),
            prefix_hash=_prefix_hash(value),
        )
    return value


def _safe_scalar(value: object) -> JsonValue | object:
    """把 exact builtin scalar 收敛为 canonical 值或稳定 marker。"""
    value_type = type(value)
    if value is None or value_type is bool:
        return cast("JsonValue", value)
    if value_type is int:
        integer = cast("int", value)
        if -_MAX_SAFE_INTEGER <= integer <= _MAX_SAFE_INTEGER:
            return integer
        return _marker(
            "integer_out_of_range",
            sign=-1 if integer < 0 else 1,
            bit_length=_bounded_count(integer.bit_length()),
        )
    if value_type is float:
        number = cast("float", value)
        return number if math.isfinite(number) else _marker("non_finite_float")
    if value_type is str:
        return _safe_string(
            cast("str", value),
            limit=_MAX_STRING_CHARS,
            too_long="string_too_long",
        )
    return _UNHANDLED


def _safe_list(
    value: list[object],
    *,
    state: _WalkState,
    depth: int,
) -> JsonValue:
    """只遍历固定前缀，并记录总长度与截断事实。"""
    length = len(value)
    result: dict[str, JsonValue] = {
        "kind": "list",
        "item_count": _bounded_count(length),
        "items": [
            _safe_input_value(item, state=state, depth=depth + 1)
            for item in value[:_MAX_CONTAINER_ENTRIES]
        ],
    }
    if length > _MAX_CONTAINER_ENTRIES:
        result["invalid"] = "container_truncated"
    return result


def _safe_mapping(
    value: dict[object, object],
    *,
    state: _WalkState,
    depth: int,
) -> JsonValue:
    """把固定数量 exact dict entries 编成稳定 entry 列表。"""
    length = len(value)
    entries: list[JsonValue] = []
    for index, (key, item) in enumerate(value.items()):
        if index == _MAX_CONTAINER_ENTRIES:
            break
        if type(key) is not str:
            return _marker(
                "non_string_mapping_key",
                entry_count=_bounded_count(length),
            )
        safe_key = _safe_string(
            key,
            limit=_MAX_KEY_CHARS,
            too_long="mapping_key_too_long",
        )
        if type(safe_key) is not str:
            return {
                **cast("dict[str, JsonValue]", safe_key),
                "entry_count": _bounded_count(length),
            }
        entries.append(
            {
                "key": safe_key,
                "value": _safe_input_value(item, state=state, depth=depth + 1),
            }
        )
    result: dict[str, JsonValue] = {
        "kind": "mapping",
        "entry_count": _bounded_count(length),
        "entries": entries,
    }
    if length > _MAX_CONTAINER_ENTRIES:
        result["invalid"] = "container_truncated"
    return result


def _safe_input_value(
    value: object,
    *,
    state: _WalkState,
    depth: int,
) -> JsonValue:
    """只读取 exact builtin，按全局/深度预算生成 total bounded JsonValue。"""
    if not state.consume():
        return _marker("node_budget")
    scalar = _safe_scalar(value)
    if scalar is not _UNHANDLED:
        return cast("JsonValue", scalar)
    if type(value) not in (list, dict):
        return _marker("unsupported_value")
    if depth >= _MAX_DEPTH:
        return _marker("depth_limit")
    identity = id(value)
    if identity in state.active:
        return _marker("cycle")
    state.active.add(identity)
    try:
        if type(value) is list:
            return _safe_list(cast("list[object]", value), state=state, depth=depth)
        return _safe_mapping(
            cast("dict[object, object]", value),
            state=state,
            depth=depth,
        )
    finally:
        state.active.remove(identity)


def safe_user_message_input_descriptor(
    submission: Submission,
) -> dict[str, JsonValue]:
    """从 exact Pydantic field storage 构造 total bounded descriptor。"""
    fields = object.__getattribute__(submission, "__dict__")
    operation = fields.get("op")
    operation_fields = (
        object.__getattribute__(operation, "__dict__")
        if type(operation) is UserMessage
        else {}
    )
    state = _WalkState()
    return {
        "schema": "audited_user_message_input_v1",
        "submission_id": _safe_input_value(
            fields.get("id"),
            state=state,
            depth=0,
        ),
        "input": _safe_input_value(
            {
                "text": operation_fields.get("text"),
                "attachments": operation_fields.get("attachments"),
            },
            state=state,
            depth=0,
        ),
    }


def user_message_input_descriptor_hash(submission: Submission) -> str:
    """计算 total bounded descriptor 的 RFC 8785 SHA-256。"""
    return canonical_hash(safe_user_message_input_descriptor(submission))


__all__ = [
    "safe_user_message_input_descriptor",
    "user_message_input_descriptor_hash",
]
