"""MultimodalEvictionStrategy —— 多模态重载荷驱逐（ADR 0082）。

旧的图片 / 文件附件换成一行描述，文本原样保留；最近的若干条带附件条目不动。
"""

from __future__ import annotations

import asyncio

import pytest

from taifeng.context.budget import ContextBudget
from taifeng.context.compressor import (
    CompressionContext,
    CompressionOrchestrator,
    CompressionStrategy,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.placeholders import EVICTED_PREFIX
from taifeng.context.strategies import MultimodalEvictionStrategy
from taifeng.conversation.models import (
    ResponseItem,
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)
from taifeng.loop.turn_helpers import _history_orphan_call_ids

TID = "t-evict"


def _image(tag: str, size: int = 40_000) -> dict[str, object]:
    return {
        "media_type": "image/png", "size": size, "sha256": f"{tag:0<64}"[:64],
        "content": "QUJD" * 10, "detail": "high",
    }


def _pdf(tag: str, size: int = 900_000) -> dict[str, object]:
    return {
        "media_type": "application/pdf", "size": size, "sha256": f"{tag:0<64}"[:64],
        "content": "UERG" * 10, "filename": f"{tag}.pdf",
    }


def _user(text: str, *attachments: dict[str, object]) -> ResponseItem:
    return user_message(text, thread_id=TID, attachments=list(attachments))


def _shot(call_id: str, *attachments: dict[str, object]) -> list[ResponseItem]:
    return [
        function_call(call_id, "screenshot", "{}", thread_id=TID),
        function_call_output(
            call_id=call_id, output=f"截图 {call_id}", thread_id=TID,
            attachments=list(attachments),
        ),
    ]


def _history() -> list[ResponseItem]:
    """四条带附件的条目（两条用户消息 + 两次截图），夹着纯文本回合。"""
    return [
        _user("看这张图", _image("a1")),                      # 0
        assistant_message("看到了", thread_id=TID, model="m"),  # 1
        *_shot("c1", _image("b1"), _image("b2")),              # 2 3
        _user("再看这份文档", _pdf("d1")),                      # 4
        assistant_message("读完了", thread_id=TID, model="m"),  # 5
        *_shot("c2", _image("e1")),                            # 6 7
        assistant_message("总结", thread_id=TID, model="m"),    # 8
    ]


def _ctx(
    history: list[ResponseItem],
    *,
    token_estimate: int = 8_000,
    window: int = 10_000,
    anchor: int = -1,
    phase: str = "pre_turn",
) -> CompressionContext:
    return CompressionContext(
        history=history,
        token_estimate=token_estimate,
        budget=ContextBudget(context_window=window),
        cache_anchor_index=anchor,
        phase=phase,  # type: ignore[arg-type]
        available_injections=frozenset({InitialContextInjection.BEFORE_LAST_USER_MESSAGE}),
    )


async def _run(strategy: MultimodalEvictionStrategy, ctx: CompressionContext):
    return await strategy.compress(ctx, InitialContextInjection.BEFORE_LAST_USER_MESSAGE)


def _attachments(item: ResponseItem) -> list[dict[str, object]]:
    return list(item.payload.get("attachments") or [])


def _strategy(**kwargs: object) -> MultimodalEvictionStrategy:
    defaults: dict[str, object] = {"keep_recent": 1, "protect_tail_messages": 0}
    return MultimodalEvictionStrategy(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_is_a_compression_strategy() -> None:
    assert isinstance(MultimodalEvictionStrategy(), CompressionStrategy)
    assert MultimodalEvictionStrategy().name == "multimodal_evict"


async def test_evicts_old_attachments_and_keeps_recent() -> None:
    history = _history()

    result = await _run(_strategy(), _ctx(history))

    assert result.success
    new = result.new_history
    assert [len(_attachments(i)) for i in new] == [0, 0, 0, 0, 0, 0, 0, 1, 0]
    assert _attachments(new[7]) == _attachments(history[7])
    assert result.detail == {
        "evicted_items": 3, "evicted_attachments": 4, "evicted_bytes": 1_020_000,
    }
    assert result.removed_item_count == 3
    # 条目不增不减、身份不变、配对完整
    assert [i.id for i in new] == [i.id for i in history]
    assert _history_orphan_call_ids(new) == set()


async def test_text_is_kept_and_stub_describes_what_was_removed() -> None:
    history = _history()

    new = (await _run(_strategy(), _ctx(history))).new_history

    user_text = new[0].payload["text"]
    assert user_text.startswith("看这张图\n")
    stub = user_text.split("\n", 1)[1]
    assert stub.startswith(EVICTED_PREFIX)
    assert "1 attachment" in stub
    assert "image/png" in stub
    assert "39.1KB" in stub
    assert "sha256=a1000000" in stub
    output = new[3].payload["output"]
    assert output.startswith("截图 c1\n")
    assert "2 attachments" in output
    assert output.count("image/png") == 2
    document = new[4].payload["text"]
    assert "application/pdf" in document
    assert "d1.pdf" in document
    assert "878.9KB" in document
    # 被驱逐的正文不再出现
    assert "QUJD" not in user_text and "UERG" not in document


async def test_payload_shape_matches_items_that_never_had_attachments() -> None:
    """驱逐后的形状与「本就没有附件」的条目逐键一致。"""
    new = (await _run(_strategy(), _ctx(_history()))).new_history

    assert new[0].payload["attachments"] == []
    assert "attachments" not in new[3].payload
    assert set(new[3].payload) == {"call_id", "output", "is_error"}


async def test_input_history_is_not_mutated() -> None:
    history = _history()
    snapshot = [i.model_dump() for i in history]

    await _run(_strategy(), _ctx(history))

    assert [i.model_dump() for i in history] == snapshot


async def test_keep_recent_counts_items_not_attachments() -> None:
    new = (await _run(_strategy(keep_recent=2), _ctx(_history()))).new_history
    assert [len(_attachments(i)) for i in new] == [0, 0, 0, 0, 1, 0, 0, 1, 0]


async def test_protected_tail_is_never_touched() -> None:
    """尾部保护范围内的附件不动，且不占 keep_recent 的名额。"""
    new = (await _run(
        _strategy(keep_recent=1, protect_tail_messages=2), _ctx(_history())
    )).new_history
    assert [len(_attachments(i)) for i in new] == [0, 0, 0, 0, 1, 0, 0, 1, 0]


async def test_cached_prefix_is_not_touched_mid_turn() -> None:
    history = _history()
    ctx = _ctx(history, anchor=3, phase="mid_turn")

    result = await _strategy().compress(ctx, InitialContextInjection.DO_NOT_INJECT)

    assert result.success
    assert [len(_attachments(i)) for i in result.new_history] == [1, 0, 0, 2, 0, 0, 0, 1, 0]
    assert result.cache_invalidated is False
    assert result.anchor_preserved_until == 3


async def test_head_eviction_is_opt_in_and_reports_cache_break() -> None:
    history = _history()
    ctx = _ctx(history, anchor=3)

    kept = await _run(_strategy(), ctx)
    crossed = await _run(_strategy(allow_head_evict=True), ctx)

    assert [len(_attachments(i)) for i in kept.new_history][:4] == [1, 0, 0, 2]
    assert kept.cache_invalidated is False
    assert [len(_attachments(i)) for i in crossed.new_history][:4] == [0, 0, 0, 0]
    assert crossed.cache_invalidated is True
    assert crossed.anchor_preserved_until == -1


async def test_head_eviction_needs_pre_turn_injection() -> None:
    history = _history()
    ctx = _ctx(history, anchor=3, phase="mid_turn")

    result = await _strategy(allow_head_evict=True).compress(
        ctx, InitialContextInjection.DO_NOT_INJECT
    )

    assert [len(_attachments(i)) for i in result.new_history][:4] == [1, 0, 0, 2]
    assert result.cache_invalidated is False


async def test_small_attachments_are_left_alone() -> None:
    history = [
        _user("小图", _image("s1", size=2_000)),
        _user("大图", _image("l1", size=200_000)),
        _user("最近", _image("r1")),
    ]

    result = await _run(_strategy(min_attachment_bytes=10_000), _ctx(history))

    assert [len(_attachments(i)) for i in result.new_history] == [1, 0, 1]
    assert result.detail["evicted_attachments"] == 1


async def test_mixed_sizes_in_one_item_evict_only_the_heavy_ones() -> None:
    history = [
        _user("两张", _image("s1", size=2_000), _image("l1", size=200_000)),
        _user("最近", _image("r1")),
    ]

    result = await _run(_strategy(min_attachment_bytes=10_000), _ctx(history))

    kept = _attachments(result.new_history[0])
    assert [a["sha256"][:2] for a in kept] == ["s1"]
    assert "1 attachment" in result.new_history[0].payload["text"]


async def test_second_run_is_a_no_op() -> None:
    first = await _run(_strategy(), _ctx(_history()))

    second = await _run(_strategy(), _ctx(first.new_history))

    assert second.success is False
    assert second.reason == "nothing_to_evict"
    assert second.new_history == []


# ------------------------------------------------------------------
# 触发
# ------------------------------------------------------------------


def test_triggers_only_under_pressure() -> None:
    strategy = _strategy(trigger_ratio=0.5)
    assert strategy.should_trigger(_ctx(_history(), token_estimate=4_000)) is None
    trigger = strategy.should_trigger(_ctx(_history(), token_estimate=6_000))
    assert trigger is not None
    assert trigger.reason == "token_limit"
    assert trigger.threshold_pct == pytest.approx(0.6)


def test_does_not_trigger_without_evictable_attachments() -> None:
    """没有可驱逐的附件时不抢占触发，让后面的策略接手。"""
    text_only = [_user("问"), assistant_message("答", thread_id=TID, model="m")]
    assert _strategy().should_trigger(_ctx(text_only)) is None
    only_recent = [_user("最近", _image("r1"))]
    assert _strategy(keep_recent=1).should_trigger(_ctx(only_recent)) is None


async def test_orchestrator_falls_through_when_nothing_to_evict() -> None:
    class _Fallback:
        name = "fallback"
        priority = 1
        ran = False

        def should_trigger(self, ctx: CompressionContext):
            from taifeng.context.compressor import CompressionTrigger

            return CompressionTrigger(reason="token_limit", threshold_pct=1.0)

        async def compress(self, ctx: CompressionContext, injection: object):
            from taifeng.context.compressor import CompressionResult

            self.ran = True
            return CompressionResult(
                success=False, cache_invalidated=False, anchor_preserved_until=-1
            )

    fallback = _Fallback()
    orchestrator = CompressionOrchestrator([_strategy(priority=50), fallback])
    text_only = [_user("问"), assistant_message("答", thread_id=TID, model="m")]

    await orchestrator.maybe_compress(
        _ctx(text_only), InitialContextInjection.BEFORE_LAST_USER_MESSAGE
    )

    assert fallback.ran


# ------------------------------------------------------------------
# 边界
# ------------------------------------------------------------------


async def test_empty_history() -> None:
    result = await _run(_strategy(), _ctx([]))
    assert result.success is False
    assert result.reason == "nothing_to_evict"


async def test_malformed_attachment_is_described_without_guessing() -> None:
    """缺字段的附件：描述里如实标 unknown，不编造类型或大小。"""
    history = [
        user_message("坏附件", thread_id=TID, attachments=[{"content": "QUJD"}]),
        _user("最近", _image("r1")),
    ]

    result = await _run(_strategy(), _ctx(history))

    text = result.new_history[0].payload["text"]
    assert "unknown type" in text
    assert "unknown size" in text
    assert result.detail["evicted_bytes"] == 0


async def test_cancellation_takes_effect() -> None:
    history = [_user(f"图 {i}", _image(f"x{i}")) for i in range(50)]
    task = asyncio.create_task(_run(_strategy(), _ctx(history)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "kwargs",
    [
        {"keep_recent": -1},
        {"protect_tail_messages": -1},
        {"min_attachment_bytes": -1},
        {"trigger_ratio": 0.0},
        {"trigger_ratio": 1.5},
    ],
)
def test_invalid_parameters_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        MultimodalEvictionStrategy(**kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# 与 prompt 组装、token 估算的衔接
# ------------------------------------------------------------------


def _real_png(seed: int) -> dict[str, object]:
    """通过 admission 的最小 PNG 附件。"""
    import base64
    import hashlib

    from taifeng.llm.image_input import ImageAttachmentV1

    data = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00" + seed.to_bytes(2, "big")
    )
    return ImageAttachmentV1(
        media_type="image/png", size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content=base64.b64encode(data).decode("ascii"), detail="high",
    ).model_dump()


async def test_evicted_images_are_not_sent_to_the_model() -> None:
    from taifeng.llm.client import ModelCapabilities
    from taifeng.llm.image_input import ImageInputPolicy
    from taifeng.llm.types import ImagePart, TextPart
    from taifeng.loop.prompt import history_to_api_messages

    policy = ImageInputPolicy(
        enabled=True, max_images=4, max_item_bytes=4096, max_total_bytes=16384,
        allowed_media_types=frozenset({"image/png"}),
    )
    capabilities = ModelCapabilities(
        input_modalities=frozenset({"text", "image"}), provider="openai", protocol="chat"
    )
    history = [
        user_message("旧图", thread_id=TID, attachments=[_real_png(1)]),
        assistant_message("看到了", thread_id=TID, model="m"),
        user_message("新图", thread_id=TID, attachments=[_real_png(2)]),
    ]

    result = await _run(_strategy(), _ctx(history))
    messages = history_to_api_messages(
        result.new_history, image_input_policy=policy, model_capabilities=capabilities,
    )

    old, new = messages[0].content, messages[2].content
    assert isinstance(old, str)
    assert old.startswith("旧图\n" + EVICTED_PREFIX)
    assert isinstance(new, list)
    assert [type(part) for part in new] == [TextPart, ImagePart]


async def test_eviction_lowers_the_token_estimate() -> None:
    from taifeng.context.budget import estimate_history_tokens

    history = [
        user_message("旧图", thread_id=TID, attachments=[_real_png(1), _real_png(2)]),
        assistant_message("看到了", thread_id=TID, model="m"),
        user_message("新图", thread_id=TID, attachments=[_real_png(3)]),
    ]
    before = estimate_history_tokens(history)

    result = await _run(_strategy(), _ctx(history))

    after = estimate_history_tokens(result.new_history)
    # 两张旧图不再计入（每张按保守口径 1500），换来的描述只有几十个 token
    assert before - after > 2_800
