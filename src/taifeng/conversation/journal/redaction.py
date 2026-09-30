"""Timeline / 导出的确定性脱敏（session-journal，ADR 0104）。

脱敏只发生在投影视图，唯一事实源不动。规则按字段名而不是按内容猜：Journal 里承载正文的
字段是有限的一组（用户与模型的文本、工具参数与结果、实发请求、附件正文……），每个被脱去的值
留下同样形状的占位——摘要与长度——外加一份清单，读者能对照原记录的 payload hash 核对。

三种视图：

- ``full``：完整内容；
- ``redacted``：正文字段换成占位，附 manifest 与原 payload hash；
- ``metadata_only``：不带 payload，显式 ``audit_complete = False``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from taifeng.conversation.journal.canonical import canonical_hash

TimelineView = Literal["full", "redacted", "metadata_only"]

_TEXT_FIELDS: frozenset[str] = frozenset({
    # 用户与模型的正文
    "text", "summary", "reason", "request_reason", "final_text", "output", "error_detail",
    # 工具与派发的输入
    "arguments", "arguments_raw", "effective_arguments", "args",
    # 实发 LLM 请求与回复
    "api_request", "normalized_items",
    # 附件正文
    "content", "data",
    # 人的答复与 peer 消息
    "resolutions", "item", "message", "request_metadata", "detail", "payload_schema",
    # 派发时的完整定义快照
    "full_definition", "then_args_template",
})
"""承载正文的字段名：在任何层级出现都脱去（附件等容器保留结构，逐项进入）。"""


@dataclass(frozen=True, slots=True)
class RedactionEntry:
    """一个被脱去的值：在 payload 里的路径与原值的摘要、长度。"""

    path: str
    sha256: str
    length: int


@dataclass(frozen=True, slots=True)
class RedactedPayload:
    """脱敏后的 payload、清单与原 payload 的 canonical hash。"""

    payload: dict[str, Any]
    manifest: tuple[RedactionEntry, ...]
    original_payload_hash: str


def _placeholder(value: Any) -> tuple[dict[str, Any], str, int]:
    """把一个值换成占位：摘要与长度取自它的 canonical 形式。"""
    digest = canonical_hash(value)
    length = len(str(value)) if isinstance(value, str) else len(digest)
    return {"redacted": True, "sha256": digest, "length": length}, digest, length


def _walk(value: Any, path: str, entries: list[RedactionEntry]) -> Any:
    """递归脱敏：命中正文字段整值替换，其余容器逐项进入。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in _TEXT_FIELDS and child is not None and child != "" and child != []:
                placeholder, digest, length = _placeholder(child)
                entries.append(RedactionEntry(child_path, digest, length))
                out[key] = placeholder
            else:
                out[key] = _walk(child, child_path, entries)
        return out
    if isinstance(value, list):
        return [_walk(child, f"{path}[{index}]", entries) for index, child in enumerate(value)]
    return value


def redact_payload(payload: dict[str, Any]) -> RedactedPayload:
    """确定性脱敏：同一 payload 永远得到同一结果。"""
    entries: list[RedactionEntry] = []
    redacted = _walk(payload, "", entries)
    return RedactedPayload(
        payload=redacted,
        manifest=tuple(entries),
        original_payload_hash=canonical_hash(payload),
    )


__all__ = ["RedactedPayload", "RedactionEntry", "TimelineView", "redact_payload"]
