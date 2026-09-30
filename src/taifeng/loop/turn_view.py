"""turn 的上下文视图：经 ContextEngine 装配每次采样发给模型的条目（ADR 0093）。

没有注入 ContextEngine 时视图就是 history 本身，本模块不做任何事。

注入之后，预算判定、压缩触发、采样请求看的都是**视图**而不是完整 history：引擎把视图压在
预算之内，压缩就不会被触发，history 里的内容也就不会被折叠掉。

视图按 history 的版本缓存（长度 + 末项 id）：同一版本只装配一次，预算判定与随后的采样
看到的是同一份。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from taifeng.context.engine import (
    AssembledContext,
    AssembleRequest,
    ContextEngineError,
    TurnUpdate,
    validate_view,
)
from taifeng.loop.event import ContextAssembled

if TYPE_CHECKING:
    from taifeng.context.engine import ContextEngine
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.turn import TurnRunner

logger = logging.getLogger(__name__)

_CACHE_BREAK_REASON = "context_engine"


class TurnContextView:
    """一个 turn 的上下文视图（持 TurnRunner 引用；缓存当前 history 版本的视图）。"""

    def __init__(self, owner: TurnRunner) -> None:
        """
        Args:
            owner: 宿主 TurnRunner —— 提供 history、预算与共享依赖。
        """
        self.__view_owner = owner
        self._version: tuple[int, str | None] | None = None
        self._assembled: AssembledContext | None = None

    @property
    def engine(self) -> ContextEngine | None:
        """注入的上下文引擎；没有注入为 None。"""
        compressors = self.__view_owner.compressors
        return None if compressors is None else compressors.context_engine

    def _current_version(self) -> tuple[int, str | None]:
        """history 的版本：长度 + 末项 id。"""
        history = self.__view_owner.history_buffer
        return len(history), history[-1].id if history else None

    def cached(self) -> AssembledContext | None:
        """当前 history 版本已装配出的视图；尚未装配、或引擎决定原样发送时为 None。"""
        if self._version != self._current_version():
            return None
        return self._assembled

    async def refresh(self) -> AssembledContext | None:
        """为当前 history 版本装配视图（同一版本只装配一次）。

        Returns:
            装配结果；没有引擎、或引擎决定原样发送完整 history 时为 None。

        Raises:
            ContextEngineError: 引擎抛出异常，或给出的视图结构不合法。
        """
        engine = self.engine
        if engine is None:
            return None
        version = self._current_version()
        if self._version == version:
            return self._assembled
        owner = self.__view_owner
        history = list(owner.history_buffer)
        try:
            assembled = await engine.assemble(AssembleRequest(
                thread_id=owner.thread_id,
                entry_skill_id=owner.entry_skill.id,
                history=tuple(history),
                budget=owner.effective_budget,
                history_tokens=owner._ctxload.estimate_items(history),  # noqa: SLF001
                cache_anchor_index=owner.cache_anchor_index,
                cancel=owner.cancel,
            ))
        except ContextEngineError:
            raise
        except Exception as exc:
            if owner.cancel.is_cancelled:
                raise
            raise ContextEngineError(
                f"context engine {engine.name!r} failed to assemble: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if assembled is not None:
            validate_view(engine.name, history, assembled)
            await self._announce(engine, history, assembled)
        self._version, self._assembled = version, assembled
        return assembled

    async def _announce(
        self, engine: ContextEngine, history: list[ResponseItem], assembled: AssembledContext,
    ) -> None:
        """打事件，并在缓存前缀被破坏时把下一次 cache 失效记为预期内（R2 / R3）。"""
        owner = self.__view_owner
        if assembled.cache_invalidated and not owner._next_cache_break_expected:  # noqa: SLF001
            owner._next_cache_break_expected = True  # noqa: SLF001
            owner._next_cache_break_reason = _CACHE_BREAK_REASON  # noqa: SLF001
        await owner._emit(ContextAssembled(data={  # noqa: SLF001
            "engine": engine.name,
            "history_items": len(history),
            "view_items": len(assembled.items),
            "view_tokens": owner._ctxload.estimate_items(list(assembled.items)),  # noqa: SLF001
            "cache_invalidated": assembled.cache_invalidated,
            "anchor_preserved_until": assembled.anchor_preserved_until,
            "detail": dict(assembled.detail),
        }))

    async def notify_turn_end(self, new_items: list[ResponseItem]) -> None:
        """一轮结束后通知引擎；尽力而为，引擎的异常记录后忽略。"""
        engine = self.engine
        if engine is None or not new_items:
            return
        owner = self.__view_owner
        try:
            await engine.after_turn(TurnUpdate(
                thread_id=owner.thread_id,
                entry_skill_id=owner.entry_skill.id,
                new_items=tuple(new_items),
                history=tuple(owner.history_buffer),
            ))
        except Exception:
            logger.exception("context engine %r after_turn failed (ignored)", engine.name)


__all__ = ["TurnContextView"]
