"""Compaction strategy 测试：边界保护 + cache anchor + 多策略协调。"""

from __future__ import annotations

import pytest

from taifeng.context.budget import ContextBudget, estimate_history_tokens
from taifeng.context.compressor import CompressionContext, CompressionOrchestrator
from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies import HandoffCompactionStrategy, SlidingWindowStrategy
from taifeng.context.strategies.handoff import _walk_back_to_safe_boundary
from taifeng.conversation.models import (
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)
from taifeng.llm.providers import MockClient, MockTurn
from taifeng.llm.types import TokenUsage


def test_walk_back_safe_boundary_preserves_tool_pair() -> None:
    """切分点不应切断 function_call → function_call_output 配对。"""
    items = [
        user_message("hi", thread_id="t"),
        assistant_message("ok", thread_id="t", model="x"),
        function_call("c1", "tool_a", "{}", thread_id="t"),
        function_call_output("c1", "out_a", thread_id="t"),
        function_call("c2", "tool_b", "{}", thread_id="t"),
        function_call_output("c2", "out_b", thread_id="t"),
    ]
    # 想从位置 3 切走前面 → c1 的 output 会孤立（caller 在 idx=2 被切走）
    new_cut = _walk_back_to_safe_boundary(items, 3)
    # 走回到 2（保留 function_call_output 与其 caller 同时被保留 / 同时被压缩）
    # 我们的算法实现：output 配对的 call 在前面 → 把 output 也保留 → cut 减小到 2
    assert new_cut == 2


def test_sliding_window_when_handoff_skipped() -> None:
    """SlidingWindowStrategy 触发在 hard limit 时。"""
    budget = ContextBudget(context_window=1000, soft_limit_ratio=0.85, hard_limit_ratio=0.95, preserve_tail_messages=2)
    sliding = SlidingWindowStrategy(keep_tail=2)
    items = [user_message(f"msg-{i} " * 30, thread_id="t") for i in range(10)]
    tokens = estimate_history_tokens(items)
    ctx = CompressionContext(
        history=items,
        token_estimate=int(budget.hard_limit) + 1,
        budget=budget,
        cache_anchor_index=-1,
        phase="mid_turn",
        available_injections=frozenset({InitialContextInjection.DO_NOT_INJECT}),
    )
    trig = sliding.should_trigger(ctx)
    assert trig is not None


@pytest.mark.asyncio
async def test_handoff_compaction_with_mock_client() -> None:
    """走通 handoff 流程：mock LLM 返回固定摘要，结果应替换中段。"""
    client = MockClient(
        turns=[MockTurn(text="## 进度\n- 已完成\n## 决策\n- 用 A 方案", usage=TokenUsage(input_tokens=400, output_tokens=20))]
    )
    strategy = HandoffCompactionStrategy(model_client=client, model="mock-model")
    budget = ContextBudget(context_window=1000, soft_limit_ratio=0.85, preserve_tail_messages=2)
    items = [user_message(f"msg-{i} {'x' * 200}", thread_id="t") for i in range(8)]
    ctx = CompressionContext(
        history=items,
        token_estimate=int(budget.soft_limit) + 100,
        budget=budget,
        cache_anchor_index=-1,
        phase="pre_turn",
        available_injections=frozenset({InitialContextInjection.BEFORE_LAST_USER_MESSAGE}),
    )
    assert strategy.should_trigger(ctx) is not None
    result = await strategy.compress(ctx, InitialContextInjection.BEFORE_LAST_USER_MESSAGE)
    assert result.success
    assert result.cache_invalidated  # pre-turn → head 改写 → invalid
    assert result.removed_item_count > 0
    # 摘要被插入
    assert any(it.kind == "compacted" for it in result.new_history)


@pytest.mark.asyncio
async def test_handoff_do_not_inject_preserves_anchor() -> None:
    """mid_turn 模式不应破坏 cache anchor 之前的内容。"""
    client = MockClient(
        turns=[MockTurn(text="## summary", usage=TokenUsage(input_tokens=100, output_tokens=10))]
    )
    strategy = HandoffCompactionStrategy(model_client=client, model="mock-model")
    budget = ContextBudget(context_window=1000, soft_limit_ratio=0.85, preserve_tail_messages=2)
    items = [user_message(f"msg-{i} {'x' * 200}", thread_id="t") for i in range(8)]
    ctx = CompressionContext(
        history=items,
        token_estimate=int(budget.soft_limit) + 100,
        budget=budget,
        cache_anchor_index=2,  # 前 3 条已 cache
        phase="mid_turn",
        available_injections=frozenset({InitialContextInjection.DO_NOT_INJECT}),
    )
    result = await strategy.compress(ctx, InitialContextInjection.DO_NOT_INJECT)
    assert result.success
    assert not result.cache_invalidated
    # cache_anchor_index=2 → compactable_start=2 → 前 2 条（索引 0, 1）原样保留
    assert result.new_history[:2] == items[:2]
    # 第 3 条应被替换为 compacted summary
    assert result.new_history[2].kind == "compacted"


_UUID = "550e8400-e29b-41d4-a716-446655440000"


def test_extract_opaque_identifiers_finds_url_uuid_hash() -> None:
    """URL / UUID / 长 hex hash 都应被识别为不可恢复标识符。"""
    from taifeng.context.strategies.handoff import _extract_opaque_identifiers

    text = (
        f"任务 {_UUID}，参见 https://example.com/a/b?x=1，"
        "提交 deadbeefcafe1234，普通词 hello 不算"
    )
    found = _extract_opaque_identifiers(text)
    assert _UUID in found
    assert any(s.startswith("https://example.com") for s in found)
    assert "deadbeefcafe1234" in found
    assert "hello" not in found


def test_audit_detects_missing_identifier() -> None:
    """源里出现的不透明标识符若不在摘要中 → 审计不合格。"""
    from taifeng.context.strategies.handoff import _audit_summary_quality

    source = [user_message(f"处理任务 {_UUID}", thread_id="t")]
    audit_bad = _audit_summary_quality("## 进度\n- 处理中（丢了 ID）", source)
    assert not audit_bad.ok
    assert _UUID in audit_bad.missing_identifiers

    audit_good = _audit_summary_quality(f"## 引用\n- {_UUID}", source)
    assert audit_good.ok
    assert not audit_good.missing_identifiers


def test_audit_missing_sections_are_nonfatal() -> None:
    """缺分段是质量警告但不致命（仅标识符丢失才致命）。"""
    from taifeng.context.strategies.handoff import _audit_summary_quality

    source = [user_message("没有任何不透明标识符的普通对话", thread_id="t")]
    audit = _audit_summary_quality("## 进度\n- 仅有这一段", source)
    assert audit.ok  # 无标识符丢失 → 合格
    assert "决策" in audit.missing_sections  # 但记录缺失分段


def _items_with_identifier() -> list:
    """构造一段含 UUID（位于可压缩区）的历史。"""
    head = user_message(f"任务 ID {_UUID} " + "数据 " * 60, thread_id="t")
    rest = [user_message(f"msg-{i} " + "x" * 200, thread_id="t") for i in range(7)]
    return [head, *rest]


def _ctx_for(items: list) -> CompressionContext:
    budget = ContextBudget(context_window=1000, soft_limit_ratio=0.85, preserve_tail_messages=2)
    return CompressionContext(
        history=items,
        token_estimate=int(budget.soft_limit) + 100,
        budget=budget,
        cache_anchor_index=-1,
        phase="pre_turn",
        available_injections=frozenset({InitialContextInjection.BEFORE_LAST_USER_MESSAGE}),
    )


@pytest.mark.asyncio
async def test_handoff_regenerates_when_identifier_dropped() -> None:
    """首轮摘要丢了 UUID → 自动重生成；第二轮补回 → 成功。"""
    _usage = TokenUsage(input_tokens=400, output_tokens=20)
    client = MockClient(turns=[
        MockTurn(text="## 进度\n- 处理中（这版丢了 ID）", usage=_usage),
        MockTurn(text=f"## 进度\n- 处理中\n## 引用\n- {_UUID}", usage=_usage),
    ])
    strategy = HandoffCompactionStrategy(model_client=client, model="mock-model")
    items = _items_with_identifier()
    result = await strategy.compress(
        _ctx_for(items), InitialContextInjection.BEFORE_LAST_USER_MESSAGE
    )
    assert result.success
    assert client._idx == 2  # noqa: SLF001 —— 确实重生成了一次
    summary = next(it for it in result.new_history if it.kind == "compacted")
    assert _UUID in summary.payload["summary"]


@pytest.mark.asyncio
async def test_handoff_fails_preserve_when_identifier_still_missing() -> None:
    """重生成后仍丢标识符 → 不压缩（保留历史），返回质量失败原因。"""
    _usage = TokenUsage(input_tokens=400, output_tokens=20)
    client = MockClient(turns=[
        MockTurn(text="## 进度\n- 丢了 ID 第 1 版", usage=_usage),
        MockTurn(text="## 进度\n- 丢了 ID 第 2 版", usage=_usage),
    ])
    strategy = HandoffCompactionStrategy(model_client=client, model="mock-model")
    items = _items_with_identifier()
    result = await strategy.compress(
        _ctx_for(items), InitialContextInjection.BEFORE_LAST_USER_MESSAGE
    )
    assert not result.success
    assert result.reason is not None and "quality" in result.reason
    assert not result.new_history  # 不改历史，调用方据此保留原 history


@pytest.mark.asyncio
async def test_orchestrator_picks_highest_priority() -> None:
    """多策略协调：优先级高的先触发。"""
    client = MockClient(turns=[MockTurn(text="## ok", usage=TokenUsage(input_tokens=50, output_tokens=5))])
    budget = ContextBudget(context_window=1000, soft_limit_ratio=0.85, preserve_tail_messages=2)
    items = [user_message(f"msg-{i} {'x'*100}", thread_id="t") for i in range(6)]
    ctx = CompressionContext(
        history=items,
        token_estimate=int(budget.soft_limit) + 50,
        budget=budget,
        cache_anchor_index=-1,
        phase="pre_turn",
        available_injections=frozenset({InitialContextInjection.BEFORE_LAST_USER_MESSAGE}),
    )
    handoff = HandoffCompactionStrategy(model_client=client, model="mock-model")
    sliding = SlidingWindowStrategy()
    orch = CompressionOrchestrator([sliding, handoff])  # 故意乱序输入，期望按 priority 排
    result = await orch.maybe_compress(ctx, InitialContextInjection.BEFORE_LAST_USER_MESSAGE)
    assert result is not None
    assert result.success


@pytest.mark.asyncio
async def test_force_compress_bypasses_should_trigger() -> None:
    """force_compress 无视 should_trigger，低估算下仍以最高优先级策略压缩。

    这是 A1 reactive-compaction-recovery 的底座：overflow 成因是「本地估算偏低、
    provider 已判超长」，此时 should_trigger 必返回 None，必须有一条绕过阈值的强制路径。
    """
    client = MockClient(
        turns=[
            MockTurn(
                text="## 进度\n- x\n## 决策\n- y",
                usage=TokenUsage(input_tokens=400, output_tokens=20),
            )
        ]
    )
    handoff = HandoffCompactionStrategy(model_client=client, model="mock-model")
    sliding = SlidingWindowStrategy()
    budget = ContextBudget(context_window=100000, soft_limit_ratio=0.85, preserve_tail_messages=2)
    items = [user_message(f"msg-{i} {'x' * 100}", thread_id="t") for i in range(8)]
    # 低估算：远低于 soft/hard limit → 两策略 should_trigger 均返回 None（常规路径压不动）
    ctx = CompressionContext(
        history=items,
        token_estimate=100,
        budget=budget,
        cache_anchor_index=-1,
        phase="mid_turn",
        available_injections=frozenset({InitialContextInjection.DO_NOT_INJECT}),
    )
    assert handoff.should_trigger(ctx) is None
    assert sliding.should_trigger(ctx) is None
    # 强制路径：取最高优先级策略（handoff priority=100 > sliding=10）执行压缩
    orch = CompressionOrchestrator([sliding, handoff])
    result = await orch.force_compress(ctx, InitialContextInjection.DO_NOT_INJECT)
    assert result is not None
    assert result.success
    # 无策略 → None（调用方据此退化为硬失败）
    empty = CompressionOrchestrator([])
    assert await empty.force_compress(ctx, InitialContextInjection.DO_NOT_INJECT) is None
