"""SessionJournal 的 RFC 8785 canonical bytes 与 SHA-256 helpers。"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, cast

import rfc8785
from pydantic import BaseModel

from taifeng.conversation.journal.errors import NonCanonicalValueError

if TYPE_CHECKING:
    from taifeng.conversation.journal.models import JournalRecord, JsonValue


def validate_json_value(value: object, *, path: str = "$") -> JsonValue:
    """递归复制并校验纯 JsonValue，拒绝隐式字符串化与非有限数。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonCanonicalValueError("float must be finite", path=path)
        return value
    if isinstance(value, list):
        return [
            validate_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonCanonicalValueError("mapping key must be str", path=path)
            result[key] = validate_json_value(item, path=f"{path}.{key}")
        return result
    raise NonCanonicalValueError(f"unsupported type {type(value).__name__}", path=path)


def canonical_bytes(value: object) -> bytes:
    """把纯 JsonValue 序列化为 RFC 8785 UTF-8 bytes。"""
    normalized = validate_json_value(value)
    try:
        return rfc8785.dumps(normalized)
    except rfc8785.CanonicalizationError as exc:
        raise NonCanonicalValueError(str(exc)) from exc


def canonical_hash(value: object) -> str:
    """计算纯 JsonValue canonical bytes 的小写 SHA-256。"""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _datetime_text(value: datetime) -> str:
    """把 aware datetime 统一为 UTC RFC 3339。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise NonCanonicalValueError("datetime must include timezone")
    utc_value = value.astimezone(UTC)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _model_value(value: object, *, path: str = "$") -> JsonValue:
    """把已登记 Journal DTO 值转换成跨实现 canonical JsonValue。"""
    if isinstance(value, BaseModel):
        return _model_value(value.model_dump(mode="python", round_trip=True), path=path)
    if isinstance(value, datetime):
        return _datetime_text(value)
    if isinstance(value, Enum):
        return _model_value(value.value, path=path)
    if isinstance(value, tuple):
        return [_model_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, list):
        return [_model_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonCanonicalValueError("mapping key must be str", path=path)
            result[key] = _model_value(item, path=f"{path}.{key}")
        return result
    return validate_json_value(value, path=path)


def model_canonical_data(model: BaseModel) -> dict[str, JsonValue]:
    """把 frozen Journal model 转成规范 dict，供 framing 与 vectors 复用。"""
    normalized = _model_value(model)
    if not isinstance(normalized, dict):  # pragma: no cover - BaseModel dump 恒为 mapping
        raise NonCanonicalValueError("model dump must be a mapping")
    return cast("dict[str, JsonValue]", normalized)


def payload_hash(payload: dict[str, JsonValue]) -> str:
    """计算 Journal payload hash。"""
    return canonical_hash(payload)


def record_fingerprint(record: JournalRecord) -> str:
    """计算完整调用方 JournalRecord 的幂等 fingerprint。"""
    return canonical_hash(model_canonical_data(record))


__all__ = [
    "canonical_bytes",
    "canonical_hash",
    "model_canonical_data",
    "payload_hash",
    "record_fingerprint",
    "validate_json_value",
]
