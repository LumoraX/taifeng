"""形状签名抽取器（sim/shape.py）单元测试。

覆盖：折叠（同 kind / 混 kind）、终止类别、字段类型类别、presence 标志、
脱敏（签名不泄漏文本）、序列化往返、类别 key、空流与单事件边界。
"""

from __future__ import annotations

from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    error,
    prompt_cache,
    rate_limits,
    reasoning_delta,
    server_model,
    structured_output,
    text_delta,
    tool_call_delta,
    tool_call_done,
)
from taifeng.llm.providers.sim.shape import (
    ShapeSignature,
    extract_shape,
    shape_class_key,
)
from taifeng.llm.types import RateLimitSnapshot, TokenUsage


def _usage() -> TokenUsage:
    """带非零值的 usage（验证签名不泄漏具体数值）。"""
    return TokenUsage(input_tokens=1234, output_tokens=567, total_tokens=1801)


def _text_stream() -> list[ResponseEvent]:
    """纯文本完成流（sim 骨架顺序）。"""
    return [
        created(),
        server_model("secret-model-name"),
        text_delta("这是一段敏感"),
        text_delta("的患者文本内容"),
        prompt_cache(cache_read=100, cache_creation=50),
        completed(response_id="resp-1", usage=_usage(), end_turn=True, request_id="req-abc"),
    ]


def test_extract_deterministic() -> None:
    """同一事件流两次抽取结果完全相等（无时间戳 / 随机量混入）。"""
    events = _text_stream()
    assert extract_shape(events) == extract_shape(events)


def test_delta_run_collapse_same_kind() -> None:
    """连续同 kind delta 折叠为单项，块数只进 chunking。"""
    sig = extract_shape(_text_stream())
    assert sig.kind_sequence == (
        "created",
        "server_model",
        "delta[text_delta]",
        "prompt_cache",
        "completed",
    )
    assert ("text_delta", 2) in sig.chunking


def test_delta_run_collapse_mixed_kind_order_insensitive() -> None:
    """混 kind 交错的 delta 段折叠为同一项——交错顺序与分块数不影响签名。"""
    base = [created(), server_model("m")]
    tail = [completed(response_id=None, usage=_usage(), end_turn=True)]
    # 两种交错顺序 + 不同分块数
    interleave_a = [
        reasoning_delta("思考"),
        text_delta("回"),
        reasoning_delta("更多"),
        text_delta("答"),
    ]
    interleave_b = [text_delta("回答整段"), reasoning_delta("思考整段")]
    sig_a = extract_shape(base + interleave_a + tail)
    sig_b = extract_shape(base + interleave_b + tail)
    assert sig_a.kind_sequence == sig_b.kind_sequence
    assert "delta[reasoning_delta,text_delta]" in sig_a.kind_sequence
    # 比对维度相等；chunking（只录不比）允许不同
    assert sig_a.comparable() == sig_b.comparable()
    assert sig_a.chunking != sig_b.chunking


def test_terminal_categories() -> None:
    """终止类别：completed / error / truncated 三态。"""
    ok = extract_shape(_text_stream())
    assert ok.terminal == "completed"

    err = extract_shape([created(), error(message="x", kind="content_filter")])
    assert err.terminal == "error"

    truncated = extract_shape([created(), server_model("m"), text_delta("半")])
    assert truncated.terminal == "truncated"

    assert extract_shape([]).terminal == "truncated"  # 空流边界


def test_field_shapes_types_and_usage_expansion() -> None:
    """字段结构记录类型类别；completed.usage 协议嵌套展开一层。"""
    sig = extract_shape(_text_stream())
    shapes = dict(sig.field_shapes)
    assert shapes["completed.usage.input_tokens"] == "int"
    assert shapes["completed.usage.cache_read_input_tokens"] == "int"
    assert shapes["completed.end_turn"] == "bool"
    assert shapes["completed.request_id"] == "str"
    assert shapes["text_delta.text"] == "str"
    # prompt_cache 四字段全在（R2 契约保护）
    for key in (
        "prompt_cache.cache_read_input_tokens",
        "prompt_cache.cache_creation_input_tokens",
        "prompt_cache.previous_cache_read_input_tokens",
        "prompt_cache.token_drop",
    ):
        assert shapes[key] == "int"


def test_structured_output_payload_not_expanded() -> None:
    """structured_output.parsed 是业务载荷——只校类型类别，不展开子字段。"""
    events = [
        created(),
        server_model("m"),
        text_delta('{"diagnosis": "x"}'),
        structured_output(parsed={"diagnosis": "敏感内容"}, raw_text='{"diagnosis": "敏感内容"}'),
        completed(response_id=None, usage=_usage(), end_turn=True),
    ]
    sig = extract_shape(events)
    shapes = dict(sig.field_shapes)
    assert shapes["structured_output.parsed"] == "dict"
    assert "structured_output.parsed.diagnosis" not in shapes


def test_type_class_union_across_events() -> None:
    """同 (kind, field) 多事件类型并集：tool_call_delta.name 首片 str、后续 null。"""
    events = [
        created(),
        tool_call_delta(call_id="c1", name="read_skill", delta='{"skill'),
        tool_call_delta(call_id="c1", name=None, delta='_id": "x"}'),
        tool_call_done(call_id="c1", name="read_skill", arguments='{"skill_id": "x"}'),
        completed(response_id=None, usage=_usage(), end_turn=False),
    ]
    shapes = dict(extract_shape(events).field_shapes)
    assert shapes["tool_call_delta.name"] == "null|str"


def test_presence_flags() -> None:
    """presence 标志覆盖语义成分出现与否与终态字段非空性。"""
    sig = extract_shape(_text_stream())
    presence = dict(sig.presence)
    assert presence == {
        "has_reasoning": False,
        "has_text": True,
        "has_tool_calls": False,
        "has_structured": False,
        "has_prompt_cache": True,
        "request_id_present": True,
        "end_turn_present": True,
    }
    # request_id=None 的流 → 存在性 False
    no_rid = extract_shape(
        [created(), completed(response_id=None, usage=_usage(), end_turn=True)]
    )
    assert dict(no_rid.presence)["request_id_present"] is False


def test_signature_leaks_no_text_or_numbers() -> None:
    """脱敏结构性保证：签名序列化全文不含文本内容 / 具体 token 数 / model 名。"""
    blob = repr(extract_shape(_text_stream()).to_dict())
    for secret in ("敏感", "患者", "secret-model-name", "1234", "567", "resp-1", "req-abc"):
        assert secret not in blob


def test_rate_limits_is_env_noise() -> None:
    """rate_limits 不进 kind 序列 / 字段结构，只记 observed_env（只录不比）。"""
    snapshot = RateLimitSnapshot()
    with_rl = [created(), rate_limits(snapshot), *_text_stream()[1:]]
    sig_with = extract_shape(with_rl)
    sig_without = extract_shape(_text_stream())
    assert sig_with.comparable() == sig_without.comparable()
    assert dict(sig_with.observed_env)["rate_limits"] is True
    assert dict(sig_without.observed_env)["rate_limits"] is False
    assert not any(k.startswith("rate_limits.") for k, _ in sig_with.field_shapes)


def test_serialization_roundtrip() -> None:
    """to_dict / from_dict 往返无损（金样 JSONL 落盘格式）。"""
    sig = extract_shape(_text_stream())
    assert ShapeSignature.from_dict(sig.to_dict()) == sig


def test_shape_class_key_expands_delta_members() -> None:
    """类别 key：delta 折叠项展开回成员 kind，附终止类别。"""
    sig = extract_shape(_text_stream())
    assert shape_class_key(sig) == (
        "completed+created+prompt_cache+server_model+text_delta->completed"
    )


def test_single_event_stream() -> None:
    """单事件边界：仅 error 事件的流。"""
    sig = extract_shape([error(message="boom", kind="server_error", retryable=True)])
    assert sig.kind_sequence == ("error",)
    assert sig.terminal == "error"
    assert dict(sig.field_shapes)["error.retryable"] == "bool"
