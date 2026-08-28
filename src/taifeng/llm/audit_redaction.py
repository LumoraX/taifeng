"""LLM attempt request 的安全投影、manifest 与 canonical digest。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from taifeng.conversation.journal.canonical import canonical_bytes

if TYPE_CHECKING:
    from taifeng.llm.types import ApiRequest

type RedactionKind = Literal["image_base64", "provider_encrypted_content"]


class SensitiveRequestShapeError(ValueError):
    """敏感字段出现在未批准结构，或 marker 会覆盖调用方数据。"""


@dataclass(frozen=True, slots=True)
class RequestRedaction:
    """一个被删除敏感值的稳定 RFC 6901 地址。"""

    path: str
    kind: RedactionKind


@dataclass(frozen=True, slots=True)
class AttemptRequestProjection:
    """observer 可见的安全 request intent 快照。"""

    api_request_safe: dict[str, Any]
    redactions: tuple[RequestRedaction, ...]
    canonical_attempt_sha256: str


def _pointer(path: tuple[str, ...]) -> str:
    """把稳定 path segments 编码为 RFC 6901 JSON Pointer。"""
    escaped = (part.replace("~", "~0").replace("/", "~1") for part in path)
    return "/" + "/".join(escaped)


def _redact_image(
    value: dict[str, Any],
    path: tuple[str, ...],
    redactions: list[RequestRedaction],
) -> dict[str, Any]:
    """删除 canonical ImagePart 正文，同时保留不可反解 descriptor。"""
    if "content_redacted" in value:
        raise SensitiveRequestShapeError("image redaction marker collision")
    body = value.get("base64_data")
    if not isinstance(body, str) or not body:
        raise SensitiveRequestShapeError("image base64_data must be non-empty")
    safe = {
        key: _redact_value(item, (*path, key), redactions)
        for key, item in value.items()
        if key != "base64_data"
    }
    safe["content_redacted"] = {"kind": "image_base64", "redacted": True}
    redactions.append(
        RequestRedaction(path=_pointer((*path, "base64_data")), kind="image_base64")
    )
    return safe


def _redact_provider_state(
    value: dict[str, Any],
    path: tuple[str, ...],
    redactions: list[RequestRedaction],
) -> dict[str, Any]:
    """只在 canonical provider_state.state.payload 中删除 ciphertext。"""
    state = value.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("payload"), dict):
        raise SensitiveRequestShapeError("provider state payload is invalid")
    payload = state["payload"]
    if "encrypted_content" not in payload:
        return {
            key: _redact_value(item, (*path, key), redactions)
            for key, item in value.items()
        }
    if "provider_state_redacted" in payload:
        raise SensitiveRequestShapeError("provider state marker collision")
    encrypted = payload.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        raise SensitiveRequestShapeError("encrypted_content must be non-empty")
    safe_payload = {
        key: _redact_value(item, (*path, "state", "payload", key), redactions)
        for key, item in payload.items()
        if key != "encrypted_content"
    }
    safe_payload["provider_state_redacted"] = {
        "kind": "provider_encrypted_content",
        "redacted": True,
    }
    redactions.append(
        RequestRedaction(
            path=_pointer((*path, "state", "payload", "encrypted_content")),
            kind="provider_encrypted_content",
        )
    )
    safe_state = {
        key: (
            safe_payload
            if key == "payload"
            else _redact_value(item, (*path, "state", key), redactions)
        )
        for key, item in state.items()
    }
    return {
        key: (
            safe_state
            if key == "state"
            else _redact_value(item, (*path, key), redactions)
        )
        for key, item in value.items()
    }


def _redact_value(
    value: Any,
    path: tuple[str, ...],
    redactions: list[RequestRedaction],
) -> Any:
    """递归复制 JSON 树；敏感 key 只允许出现在批准的 typed item。"""
    if isinstance(value, list):
        return [
            _redact_value(item, (*path, str(index)), redactions)
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "image" and "base64_data" in value:
        return _redact_image(value, path, redactions)
    if value.get("type") == "provider_state":
        return _redact_provider_state(value, path, redactions)
    if "base64_data" in value or "encrypted_content" in value:
        raise SensitiveRequestShapeError("sensitive key is outside approved shape")
    return {
        key: _redact_value(item, (*path, key), redactions)
        for key, item in value.items()
    }


def project_attempt_request(
    provider: str,
    model: str,
    request: ApiRequest,
) -> AttemptRequestProjection:
    """在内存中生成安全投影，并绑定脱敏前 provider-neutral request。"""
    full = request.model_dump(mode="json")
    digest = hashlib.sha256(
        canonical_bytes(
            {"provider": provider, "model": model, "api_request": full}
        )
    ).hexdigest()
    redactions: list[RequestRedaction] = []
    safe = _redact_value(full, (), redactions)
    assert isinstance(safe, dict)
    ordered = tuple(
        sorted(redactions, key=lambda entry: entry.path.encode("utf-8"))
    )
    paths = [entry.path for entry in ordered]
    if len(paths) != len(set(paths)):
        raise SensitiveRequestShapeError("redaction paths must be unique")
    return AttemptRequestProjection(
        api_request_safe=safe,
        redactions=ordered,
        canonical_attempt_sha256=digest,
    )


__all__ = [
    "AttemptRequestProjection",
    "RequestRedaction",
    "SensitiveRequestShapeError",
    "project_attempt_request",
]
