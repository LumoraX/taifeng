"""Journal 回放的请求匹配：采样 id 重命名下的两段式摘要比对（ADR 0070）。

Responses 协议的输入项带 ``sample_id`` / ``origin_sample_id``，形如
``{thread_id}:{submission_id}:turn:{turn_index}:llm:{iteration}``（派生规则见
``loop/turn_helpers._responses_sample_id``）。thread id 与 submission id 每次运行都不同，
因此录制侧的 ``canonical_attempt_sha256`` 不能在回放中直接复算。

这些采样 id **从不进入 provider wire**——它们只在内核里把同一次响应的 reasoning / 消息 /
工具调用归组、把工具结果挂回来源采样。所以「两次请求对模型而言相同」的精确判据是：
**在采样 id 的一致双射重命名下逐字节相同**。匹配分两段：

1. **定位**：把请求安全投影（与 Journal 里的 ``api_request_safe`` 同形）中符合派生语法的采样 id
   按首次出现顺序换成占位符，算摘要，按它找候选录制调用；
2. **复核**：用「本次采样 id → 录制采样 id」的位置映射把**完整**请求（含被脱敏的图片正文与
   provider 密文）改写回录制时的 id，再按录制侧同一 preimage 复算
   ``canonical_attempt_sha256``，必须与录制值逐字节相等。

第二段保证不因脱敏或重命名放宽匹配：密文 / 图片正文不同、采样归组不同，都会复核失败。
``legacy:`` 前缀等不符合派生语法的 id 本就确定，不参与重命名。

参照：α 等价（alpha-equivalence）——绑定名不同、结构相同即视为同一项。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from taifeng.llm.audit_redaction import canonical_attempt_digest

if TYPE_CHECKING:
    from collections.abc import Mapping

# 输入项中承载采样 id 的字段（ApiMessageItem / ApiFunctionCallItem / ApiProviderStateItem 的
# sample_id，ApiFunctionCallOutputItem 的 origin_sample_id）
_SAMPLE_FIELDS = frozenset({"sample_id", "origin_sample_id"})
# 与 loop/turn_helpers._responses_sample_id 的派生规则同步（测试守护两处一致）
_RUN_DERIVED_SAMPLE_ID = re.compile(r"^.+:turn:\d+:llm:\d+$")
_PLACEHOLDER = "~sample:{index}"


class ReplayRequestShapeError(ValueError):
    """请求 JSON 缺少可回放的 ``input_items`` 列表。"""


def _input_items(api_request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """取请求的有序输入项；形状不对显式报错（不当作空请求）。"""
    items = api_request.get("input_items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ReplayRequestShapeError("api request must carry an input_items list of objects")
    return items


def run_derived_sample_ids(api_request: Mapping[str, Any]) -> tuple[str, ...]:
    """按首次出现顺序列出请求里由运行身份派生的采样 id。"""
    ordered: dict[str, None] = {}
    for item in _input_items(api_request):
        for key in sorted(_SAMPLE_FIELDS & item.keys()):
            value = item[key]
            if isinstance(value, str) and _RUN_DERIVED_SAMPLE_ID.fullmatch(value):
                ordered.setdefault(value, None)
    return tuple(ordered)


def rename_sample_ids(
    api_request: Mapping[str, Any], mapping: Mapping[str, str],
) -> dict[str, Any]:
    """复制请求 JSON，把输入项采样字段中出现在 ``mapping`` 里的 id 换成映射值。"""
    renamed_items = [
        {
            key: mapping.get(value, value)
            if key in _SAMPLE_FIELDS and isinstance(value, str) else value
            for key, value in item.items()
        }
        for item in _input_items(api_request)
    ]
    return {**api_request, "input_items": renamed_items}


def locator_digest(
    provider: str, model: str, api_request_safe: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    """第一段：采样 id 换成位置占位符后的安全投影摘要，及原采样 id 顺序。

    Returns:
        (定位摘要, 按首次出现顺序的原采样 id)。
    """
    sample_ids = run_derived_sample_ids(api_request_safe)
    placeholders = {
        sample_id: _PLACEHOLDER.format(index=index) for index, sample_id in enumerate(sample_ids)
    }
    normalized = rename_sample_ids(api_request_safe, placeholders)
    return canonical_attempt_digest(provider, model, normalized), sample_ids


def matches_recorded_digest(
    provider: str,
    model: str,
    api_request_full: Mapping[str, Any],
    recorded_sample_ids: tuple[str, ...],
    recorded_digest: str,
) -> bool:
    """第二段：完整请求改写回录制采样 id 后，摘要须与录制 ``canonical_attempt_sha256`` 相等。"""
    sample_ids = run_derived_sample_ids(api_request_full)
    if len(sample_ids) != len(recorded_sample_ids):
        return False
    mapping = dict(zip(sample_ids, recorded_sample_ids, strict=True))
    rewritten = rename_sample_ids(api_request_full, mapping)
    return canonical_attempt_digest(provider, model, rewritten) == recorded_digest


__all__ = [
    "ReplayRequestShapeError",
    "locator_digest",
    "matches_recorded_digest",
    "rename_sample_ids",
    "run_derived_sample_ids",
]
