"""出站消息归一化 —— root turn 的最终回答在交给业务之前经 hook 归一（ADR 0086）。

模型的最终回答要被业务送到各种出口：聊天窗口、消息渠道、工单系统。送出去之前往往要做
同一类处理——去掉漏进正文的推理标签、规整换行与空白、脱敏、加签名。此前业务只能自己拼接
``assistant_text`` 增量再处理；内核没有「这一轮的最终回答」这个事件，也没有改写它的入口。

本模块提供：

- hook 类型 ``outbound_message``：handler 返回 ``HookDecision.ok(text_override="...")`` 即改写
  出站文本；多个 handler 链式生效。与 ``post_tool_use`` 的 ``output_override`` 同构（ADR 0061）；
- 事件 ``outbound_message``：携带归一后的文本，在 ``turn_completed`` 之前发出；
- 内核自带的归一化 ``normalize_outbound_text`` 与把它包成 handler 的
  ``make_outbound_normalizer_hook``（opt-in，业务自行注册）。

边界：

- **只改出站文本，不改 history**。模型的原话原样留在 history 与 transcript 里，缓存前缀不受影响；
- **只对 root turn**。子 skill、detached spawn 的结果是给模型或聚合 skill 看的，不是出站消息；
- **只对真终态**。挂起的 turn 没有最终回答；
- **未注册 handler 时不发事件**，事件流与引入前一致；
- hook 不可否决：回答已经产生。handler 抛异常、拒绝、或给出非字符串的改写时记错误日志，
  文本保持上一步的结果。需要「失败即不放行」的脱敏应自行捕获异常并返回安全的改写。

参照：openclaw ``infra/outbound`` 的 payload 归一化。差异：渠道适配（分片、富文本转换）是产品
层的事，内核只给出归一入口与渠道无关的默认规则。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from taifeng.hooks.types import HookContext, HookDecision, OutboundMessageHook
from taifeng.loop.event import OutboundMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.loop.turn import TurnOutcome, TurnRunner

logger = logging.getLogger(__name__)

# 漏进正文的推理块：成对的 <think> / <thinking> 标签（不区分大小写，可跨行）
_REASONING_BLOCK = re.compile(r"<(think|thinking)>.*?</\1>", re.IGNORECASE | re.DOTALL)
# 围栏代码块：其内部原样保留
_CODE_FENCE = re.compile(r"(```.*?```)", re.DOTALL)
_TRAILING_WHITESPACE = re.compile(r"[ \t]+(?=\n)|[ \t]+$")
_BLANK_RUN = re.compile(r"\n{3,}")


def _normalize_prose(text: str, notes: list[str]) -> str:
    """规整代码块之外的一段文本；做过的处理记进 ``notes``（去重、保序）。"""

    def apply(label: str, new: str, old: str) -> str:
        if new != old and label not in notes:
            notes.append(label)
        return new

    text = apply("reasoning_block", _REASONING_BLOCK.sub("", text), text)
    text = apply("newlines", text.replace("\r\n", "\n").replace("\r", "\n"), text)
    text = apply("trailing_whitespace", _TRAILING_WHITESPACE.sub("", text), text)
    return apply("blank_lines", _BLANK_RUN.sub("\n\n", text), text)


def normalize_outbound_text(text: str) -> tuple[str, tuple[str, ...]]:
    """内核自带的出站归一化；返回 ``(归一后的文本, 做过的处理)``。

    规则（只作用于围栏代码块之外）：

    - ``reasoning_block``：去掉成对的 ``<think>`` / ``<thinking>`` 块（未闭合的不动）；
    - ``newlines``：CRLF / CR 统一为 LF；
    - ``trailing_whitespace``：去掉行尾空白；
    - ``blank_lines``：三个及以上连续换行压成两个；
    - ``trimmed``：去掉首尾空白。

    幂等：对结果再归一一次不产生任何处理。
    """
    notes: list[str] = []
    parts = _CODE_FENCE.split(text)
    # split 带捕获组：奇数下标是代码块，原样保留
    normalized = "".join(
        part if index % 2 else _normalize_prose(part, notes)
        for index, part in enumerate(parts)
    )
    stripped = normalized.strip()
    if stripped != normalized:
        notes.append("trimmed")
    return stripped, tuple(notes)


def make_outbound_normalizer_hook() -> Callable[
    [OutboundMessageHook, HookContext], Awaitable[HookDecision]
]:
    """把内核自带的归一化包成 ``outbound_message`` handler（业务自行注册）。"""

    async def handler(hook: OutboundMessageHook, ctx: HookContext) -> HookDecision:
        """归一出站文本；没有变化时不给改写。"""
        text, notes = normalize_outbound_text(hook.text)
        if not notes:
            return HookDecision.ok()
        return HookDecision.ok(text_override=text, normalized=list(notes))

    return handler


async def _apply_handlers(
    handlers: list[Any], hook: OutboundMessageHook, ctx: HookContext,
) -> str:
    """串行跑全部 handler，返回最终文本；后一个 handler 看到前一个改写后的文本。"""
    text = hook.text
    for handler in handlers:
        current = OutboundMessageHook(
            text=text, end_reason=hook.end_reason, success=hook.success,
            iteration=hook.iteration,
        )
        try:
            decision = await handler(current, ctx)
        except Exception:
            logger.exception("outbound_message hook raised; text unchanged")
            continue
        if not decision.allow:
            logger.error(
                "outbound_message hook cannot veto an answer (reason=%s); text unchanged",
                decision.reason,
            )
            continue
        override = decision.metadata.get("text_override")
        if override is None:
            continue
        if not isinstance(override, str):
            logger.error(
                "outbound_message hook gave a non-string text_override (%s); text unchanged",
                type(override).__name__,
            )
            continue
        text = override
    return text


async def emit_outbound(runner: TurnRunner, outcome: TurnOutcome, *, is_root: bool) -> None:
    """root turn 到达真终态时归一最终回答并发 ``outbound_message``（先于 ``turn_completed``）。

    未配置 hook、没有注册 ``outbound_message`` handler、不是 root turn、或 turn 挂起时什么都不做。
    """
    if not is_root or runner.hooks is None or outcome.end_reason == "suspended":
        return
    handlers = runner.hooks.registry.handlers("outbound_message")
    if not handlers:
        return
    hook = OutboundMessageHook(
        text=outcome.final_text, end_reason=outcome.end_reason, success=outcome.success,
        iteration=outcome.iterations,
    )
    ctx = HookContext(
        thread_id=runner.thread_id, submission_id=runner.submission_id,
        entry_skill_id=runner.entry_skill.id,
    )
    text = await _apply_handlers(handlers, hook, ctx)
    await runner._emit(OutboundMessage(data={  # noqa: SLF001
        "text": text,
        "rewritten": text != outcome.final_text,
        "raw_chars": len(outcome.final_text),
        "end_reason": outcome.end_reason,
        "success": outcome.success,
        "thread_id": runner.thread_id,
    }))


__all__ = [
    "emit_outbound",
    "make_outbound_normalizer_hook",
    "normalize_outbound_text",
]
