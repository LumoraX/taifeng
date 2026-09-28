"""Anthropic extended thinking：请求配置、流式累积与签名回传（thinking-passback）。

Anthropic 开启 extended thinking 后：

1. 响应流里出现 ``thinking`` / ``redacted_thinking`` content block：
   ``thinking_delta`` 携带可读思考文本，``signature_delta`` 携带完整性签名；
   ``redacted_thinking`` 块只有加密 ``data``。
2. 工具调用续传时，上一条 assistant 消息 **必须** 以这些块原样（含签名）开头，
   否则 API 以 400 拒绝——只回传纯文本 reasoning 不够。

本模块把这些块在流末打包成不透明状态 ``{"anthropic": {"blocks": [...]}}``，经
``reasoning_state`` 事件交给内核落史；下次构建请求时由 ``prepend_thinking_blocks``
取回，放回 assistant content 开头。内核只搬运、不解析（R1）。

参照：Anthropic 官方文档 extended thinking（「Preserving thinking blocks」）；
hermes-agent ``agent/anthropic_adapter.py`` 的 thinking 块保留。
"""

from __future__ import annotations

from typing import Any

from taifeng.llm.errors import InvalidRequestError

# Anthropic 规定的最小 thinking 预算
MIN_THINKING_BUDGET = 1024
# 请求未显式给 max_tokens 时，在 thinking 预算之上留给正文 / 工具调用的额度
_ANSWER_HEADROOM = 4096
# reasoning_effort → thinking 预算（请求级覆盖；none / minimal = 不开）
_EFFORT_BUDGETS = {"low": 2048, "medium": 8192, "high": 24576}
# 状态在不透明 dict 里的顶层键
STATE_KEY = "anthropic"


def resolve_thinking_budget(
    client_budget: int | None, reasoning_effort: str | None,
) -> int | None:
    """决定本次请求的 thinking 预算。

    请求级 ``reasoning_effort`` 显式给出时优先（``none`` / ``minimal`` 表示关闭）；
    否则用客户端构造时的 ``thinking_budget_tokens``。

    Raises:
        InvalidRequestError: 预算低于 Anthropic 下限 1024。
    """
    if reasoning_effort is not None:
        budget = _EFFORT_BUDGETS.get(reasoning_effort)
    else:
        budget = client_budget
    if budget is not None and budget < MIN_THINKING_BUDGET:
        raise InvalidRequestError(
            f"anthropic thinking budget must be >= {MIN_THINKING_BUDGET}, got {budget}")
    return budget


def apply_thinking_config(
    payload: dict[str, Any], *, budget: int, requested_max_tokens: int | None,
) -> None:
    """把 thinking 配置写进请求 payload（就地修改）。

    - ``thinking = {"type": "enabled", "budget_tokens": budget}``；
    - ``max_tokens`` 必须大于预算：请求未指定时自动取「预算 + 正文额度」；
      显式指定却不大于预算 → 报错（显式冲突不替业务改值）；
    - Anthropic 开 thinking 时不接受自定义 temperature → 显式报错而非静默丢弃。

    Raises:
        InvalidRequestError: 显式 max_tokens 不大于预算，或同时设置了 temperature。
    """
    if "temperature" in payload:
        raise InvalidRequestError(
            "anthropic extended thinking does not accept a custom temperature")
    if requested_max_tokens is None:
        payload["max_tokens"] = budget + _ANSWER_HEADROOM
    elif requested_max_tokens <= budget:
        raise InvalidRequestError(
            f"max_output_tokens ({requested_max_tokens}) must exceed the thinking "
            f"budget ({budget})")
    payload["thinking"] = {"type": "enabled", "budget_tokens": budget}


class ThinkingAccumulator:
    """按 content block index 累积 thinking / redacted_thinking 块。"""

    def __init__(self) -> None:
        """空累加器；一次流一个实例。"""
        self._blocks: dict[int, dict[str, Any]] = {}

    def start(self, index: int, block: dict[str, Any]) -> bool:
        """处理 ``content_block_start``；是 thinking 类块则登记并返回 True。"""
        btype = block.get("type")
        if btype == "thinking":
            self._blocks[index] = {
                "type": "thinking",
                "thinking": str(block.get("thinking") or ""),
                "signature": str(block.get("signature") or ""),
            }
            return True
        if btype == "redacted_thinking":
            self._blocks[index] = {"type": "redacted_thinking", "data": str(block.get("data") or "")}
            return True
        return False

    def delta(self, index: int, delta: dict[str, Any]) -> str | None:
        """处理 thinking 类 ``content_block_delta``；返回应作为 reasoning_delta 输出的文本。"""
        acc = self._blocks.get(index)
        if acc is None:
            return None
        dtype = delta.get("type")
        if dtype == "thinking_delta":
            text = str(delta.get("thinking") or "")
            acc["thinking"] = acc.get("thinking", "") + text
            return text or None
        if dtype == "signature_delta":
            # 签名整段下发（非增量拼接）；以最后一次为准
            acc["signature"] = str(delta.get("signature") or "")
        return None

    def state(self) -> dict[str, Any] | None:
        """流末打包：按 block index 排序的块列表；没有 thinking 块时为 None。"""
        if not self._blocks:
            return None
        blocks = [self._blocks[i] for i in sorted(self._blocks)]
        return {STATE_KEY: {"blocks": blocks}}


def thinking_blocks_from_state(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从不透明 reasoning 状态里取回本 provider 的 thinking 块（形状不对则为空）。"""
    if not state:
        return []
    mine = state.get(STATE_KEY)
    if not isinstance(mine, dict):
        return []
    blocks = mine.get("blocks")
    if not isinstance(blocks, list):
        return []
    return [dict(b) for b in blocks if isinstance(b, dict) and b.get("type") in (
        "thinking", "redacted_thinking")]


__all__ = [
    "MIN_THINKING_BUDGET",
    "ThinkingAccumulator",
    "apply_thinking_config",
    "resolve_thinking_budget",
    "thinking_blocks_from_state",
]
