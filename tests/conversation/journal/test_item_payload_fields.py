"""Wave 3 复现:audit journal 的 conversation item payload 缺字段导致合法 item 被拒。

``_FunctionCallItemPayload`` / ``_FunctionCallOutputItemPayload`` 是 extra="forbid",
却分别缺 ``extra_content``（models.function_call 会写）与 ``attachments``
（models.function_call_output 的图片附件）—— audit 模式下一条带图片的工具结果
就会 ValidationError → 冻结整个 session。
"""

from __future__ import annotations

from taifeng.conversation.models import function_call, function_call_output

TID = "t-journal"


def _validate(item: object) -> object:
    """走 journal 的 item payload 校验（与 durable 写入同一入口）。"""
    from taifeng.conversation.journal import records

    kind = item.kind  # type: ignore[attr-defined]
    payload = item.payload  # type: ignore[attr-defined]
    if kind == "function_call":
        return records._FunctionCallItemPayload.model_validate(payload)  # noqa: SLF001
    return records._FunctionCallOutputItemPayload.model_validate(payload)  # noqa: SLF001


def test_function_call_with_extra_content_is_durable() -> None:
    """provider 专属 extra_content 必须能落 durable，不得被判非法。"""
    item = function_call(
        call_id="c1", name="echo", arguments="{}", thread_id=TID,
        extra_content={"vendor": {"trace": "x"}},
    )
    model = _validate(item)
    assert model.extra_content == {"vendor": {"trace": "x"}}  # type: ignore[attr-defined]


def test_function_call_output_with_attachments_is_durable() -> None:
    """带图片附件的工具结果必须能落 durable（此前直接冻结 session）。"""
    item = function_call_output(
        call_id="c1", output="见图", thread_id=TID,
        attachments=[{
            "kind": "image", "media_type": "image/png",
            "data": "iVBORw0KGgo=", "sha256": "a" * 64,
        }],
    )
    model = _validate(item)
    assert len(model.attachments) == 1  # type: ignore[attr-defined]


def test_absent_optional_keys_keep_legacy_shape() -> None:
    """缺省时不写键（冷恢复重放与审计比对依赖逐键形状）。"""
    fc = function_call(call_id="c2", name="echo", arguments="{}", thread_id=TID)
    fco = function_call_output(call_id="c2", output="ok", thread_id=TID)
    assert "extra_content" not in fc.payload
    assert "attachments" not in fco.payload
    assert _validate(fc).extra_content is None  # type: ignore[attr-defined]
    assert _validate(fco).attachments is None  # type: ignore[attr-defined]
