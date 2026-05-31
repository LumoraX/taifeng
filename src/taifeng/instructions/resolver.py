"""InstructionResolver —— 多层指令解析 + 缓存 + 取消 + 可观测。

参照：codex codex-rs/core/src/agents_md.rs（只学"分层 + 合并"的范式，不抄文件读取）。

设计要点：
    - 静态 source（``str``）→ ``source_kind='static'``，永不重新拉取。
    - 动态 source（``InstructionSource`` 实例）→ ``source_kind='dynamic'``，
      受 ``cache_ttl_seconds`` 控制；缓存键 ``(name, session_id, entry_skill_id)``。
    - fetch 失败 → 包成 ``InstructionFetchError`` 上抛（fail-fast，禁止 silent fallback）。
    - fetch 抛 ``asyncio.CancelledError`` 时 SHALL 透传（不包成 InstructionFetchError）。
    - 取消传播：每次 fetch 前 `ctx.cancel.raise_if_cancelled()`；fetch 期间靠业务侧
      自行 await cancel scope。
    - 每次 fetch / cache hit / failure 通过注入的 ``emit`` 回调发 EventMsg。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taifeng.instructions.source import InstructionFetchError, InstructionSource
from taifeng.instructions.types import (
    InstructionContext,
    InstructionLayer,
    InstructionScope,
    ResolvedInstruction,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# 单条指令文本上限（spec: text > 10MB SHALL raise InstructionFetchError）
_MAX_TEXT_BYTES = 10 * 1024 * 1024


@dataclass
class _CacheEntry:
    """动态 source 的缓存条目。

    Attributes:
        text: fetch 返回的文本（None 表示业务侧主动选择本次不注入）。
        fetched_at: epoch seconds（time.time()）。
    """

    text: str | None
    fetched_at: float


class InstructionResolver:
    """多层指令解析器 —— 按 scope 过滤 + 按 priority 升序拼接。

    生命周期：通常被 ``AgentEngine`` 持有；engine scope 在 ``EnginePool.create``
    时一次性解析；session / turn scope 在对应生命周期边界解析（受缓存控制）。

    并发：本类自身**非线程安全**；外部使用方（engine 主 actor）保证串行化。

    Args:
        layers: 业务侧传入的层列表；同名 layer SHALL raise ValueError。
        emit: 可选的 EventMsg 异步回调（通常注入 engine._emit）；None 时静默。
    """

    def __init__(
        self,
        layers: list[InstructionLayer],
        *,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        # 边界：同名 layer 立即 raise（spec: 构造时 SHALL raise ValueError）
        seen: set[str] = set()
        for layer in layers:
            if layer.name in seen:
                raise ValueError(
                    f"duplicate InstructionLayer name: {layer.name!r}"
                )
            seen.add(layer.name)

        # 复制一份 —— 业务侧后续修改外部 list 不影响内部
        self._layers: list[InstructionLayer] = list(layers)
        self._emit = emit
        # 动态 source 缓存：(layer_name, session_id, entry_skill_id) → entry
        self._cache: dict[tuple[str, str, str], _CacheEntry] = {}
        # 最近一次 resolve 出的快照（按 priority 升序）—— snapshot() 返回副本
        self._last_snapshot: list[ResolvedInstruction] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def replace_layer(self, layer_name: str, new_source: Any) -> bool:
        """热更：替换指定 name 的 layer 的 source；同时清除该 layer 的缓存。

        Returns:
            True 表示找到并替换；False 表示未知 name（业务侧应发 rejected 事件）。
        """
        for i, layer in enumerate(self._layers):
            if layer.name != layer_name:
                continue
            # frozen dataclass → 用 dataclasses.replace
            from dataclasses import replace
            self._layers[i] = replace(layer, source=new_source)
            # 失效该 layer 的所有缓存条目
            self._invalidate_layer_cache(layer_name)
            return True
        return False

    def _invalidate_layer_cache(self, layer_name: str) -> None:
        """清除指定 layer 在所有 (session, skill) 组合下的缓存条目。"""
        stale = [k for k in self._cache if k[0] == layer_name]
        for k in stale:
            self._cache.pop(k, None)

    def snapshot(self) -> list[ResolvedInstruction]:
        """返回最近一次 resolve 结果的副本（按 priority 升序）。

        spec Requirement: snapshot 不可变（frozen dataclass + list 副本）。
        """
        return list(self._last_snapshot)

    def has_scope(self, scope: InstructionScope) -> bool:
        """是否存在指定 scope 的 layer（业务侧/engine 优化路径用）。"""
        return any(layer.scope == scope for layer in self._layers)

    async def resolve(
        self,
        scope: InstructionScope | tuple[InstructionScope, ...],
        ctx: InstructionContext,
    ) -> list[ResolvedInstruction]:
        """解析指定 scope（或多个 scope）的所有 layer。

        Args:
            scope: 单个 scope 或 scope 元组；元组按 ``layer.scope ∈ 元组`` 过滤。
            ctx: 传给业务侧 fetch 的上下文（含 cancel token）。

        Returns:
            ResolvedInstruction 列表（按 priority 升序；稳定排序，priority 相等
            按 ``layers`` 原始顺序）。

        Raises:
            InstructionFetchError: 任一 fetch 抛非取消异常时。
            asyncio.CancelledError: 取消时透传（不包装）。
        """
        targets = (scope,) if isinstance(scope, str) else tuple(scope)
        # 过滤候选；保持原始顺序（稳定排序）
        candidates = [
            layer for layer in self._layers if layer.scope in targets
        ]
        # 按 priority 升序（Python sorted 是稳定的）
        candidates.sort(key=lambda layer: layer.priority)

        resolved: list[ResolvedInstruction] = []
        for layer in candidates:
            item = await self._resolve_one(layer, ctx)
            if item is not None:
                resolved.append(item)
        # 更新内部 snapshot（合并 engine + session + turn 的全局视图由
        # 调用方在外面拼接；这里只更新本次 resolve 的子集，但调用方
        # 一般会传 ('engine','session','turn') 三档一起 resolve）
        self._last_snapshot = list(resolved)
        return resolved

    # ------------------------------------------------------------------
    # Internal: per-layer
    # ------------------------------------------------------------------

    async def _resolve_one(
        self,
        layer: InstructionLayer,
        ctx: InstructionContext,
    ) -> ResolvedInstruction | None:
        """解析单层。

        - 静态 source（str）→ 直接返回。
        - 动态 source（Protocol）→ 命中缓存走缓存；否则 fetch + 入缓存。
        - fetch 返回 None → 本次不注入（None 也入缓存，避免反复尝试）。
        - fetch 抛 CancelledError → 透传。
        - fetch 抛其他异常 → 发 instruction_fetch_failed 事件 + raise InstructionFetchError。
        """
        # 取消传播：先检查 ctx.cancel
        cancel = ctx.cancel
        if cancel is not None and getattr(cancel, "is_cancelled", False):
            cancel.raise_if_cancelled()

        # 静态分支
        if isinstance(layer.source, str):
            text = layer.source
            self._check_text_size(layer.name, text)
            return ResolvedInstruction(
                name=layer.name,
                scope=layer.scope,
                text=text,
                fetched_at=time.time(),
                source_kind="static",
                cache_hit=False,
                cache_volatile=layer.cache_volatile,
            )

        # 动态分支
        if not isinstance(layer.source, InstructionSource):
            raise TypeError(
                f"InstructionLayer({layer.name!r}).source must be str or "
                f"InstructionSource, got {type(layer.source).__name__}"
            )
        return await self._resolve_dynamic(layer, ctx)

    async def _resolve_dynamic(
        self,
        layer: InstructionLayer,
        ctx: InstructionContext,
    ) -> ResolvedInstruction | None:
        """动态 source：缓存命中 → 走缓存；否则 fetch。"""
        cache_key = (layer.name, ctx.session_id, ctx.entry_skill_id)
        entry = self._cache.get(cache_key)
        now = time.time()

        # 缓存命中（ttl > 0 且未过期）
        if (
            entry is not None
            and layer.cache_ttl_seconds > 0
            and (now - entry.fetched_at) < layer.cache_ttl_seconds
        ):
            await self._emit_event(
                "instruction_cache_hit",
                {
                    "layer_name": layer.name,
                    "cache_age_seconds": now - entry.fetched_at,
                },
            )
            if entry.text is None:
                return None
            return ResolvedInstruction(
                name=layer.name,
                scope=layer.scope,
                text=entry.text,
                fetched_at=entry.fetched_at,
                source_kind="dynamic",
                cache_hit=True,
                cache_volatile=layer.cache_volatile,
            )

        # 真实 fetch
        text = await self._do_fetch(layer, ctx)
        fetched_at = time.time()
        # 入缓存（即使 text=None 也入；ttl=0 也入但下次必过期）
        self._cache[cache_key] = _CacheEntry(text=text, fetched_at=fetched_at)

        if text is None:
            return None
        self._check_text_size(layer.name, text)
        return ResolvedInstruction(
            name=layer.name,
            scope=layer.scope,
            text=text,
            fetched_at=fetched_at,
            source_kind="dynamic",
            cache_hit=False,
            cache_volatile=layer.cache_volatile,
        )

    async def _do_fetch(
        self,
        layer: InstructionLayer,
        ctx: InstructionContext,
    ) -> str | None:
        """真实调用业务侧 fetch；失败 fail-fast；CancelledError 透传。"""
        start = time.monotonic()
        try:
            raw = await layer.source.fetch(ctx)
        except BaseException as e:
            # CancelledError / KeyboardInterrupt 透传
            import asyncio
            if isinstance(e, asyncio.CancelledError):
                # spec: 取消不发 instruction_fetch_failed 事件
                raise
            if not isinstance(e, Exception):
                # SystemExit / KeyboardInterrupt 等 BaseException 子类
                raise
            await self._emit_event(
                "instruction_fetch_failed",
                {"layer_name": layer.name, "cause_repr": repr(e)},
            )
            raise InstructionFetchError(layer_name=layer.name, cause=e) from e

        # 类型校验：业务侧 fetch 应返回 str | None
        text: str | None
        if raw is None:
            text = None
        elif isinstance(raw, str):
            text = raw
        else:
            cause = TypeError(
                f"InstructionSource.fetch must return str | None, "
                f"got {type(raw).__name__}"
            )
            await self._emit_event(
                "instruction_fetch_failed",
                {"layer_name": layer.name, "cause_repr": repr(cause)},
            )
            raise InstructionFetchError(layer_name=layer.name, cause=cause)

        duration_ms = int((time.monotonic() - start) * 1000)
        await self._emit_event(
            "instruction_fetched",
            {
                "layer_name": layer.name,
                "scope": layer.scope,
                "duration_ms": duration_ms,
                "text_length": len(text) if text else 0,
            },
        )
        return text

    def _check_text_size(self, layer_name: str, text: str) -> None:
        """spec 边界: text > 10MB SHALL raise InstructionFetchError。"""
        if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
            cause = ValueError(
                f"instruction text exceeds {_MAX_TEXT_BYTES} bytes "
                f"(got {len(text)} chars)"
            )
            raise InstructionFetchError(layer_name=layer_name, cause=cause)

    async def _emit_event(self, kind: str, data: dict[str, Any]) -> None:
        """通过注入的 emit 回调发 EventMsg；emit=None 则静默。"""
        if self._emit is None:
            return
        try:
            await self._emit(kind, data)
        except Exception:
            # telemetry 异常不能影响主 turn；记日志即可
            logger.exception("instruction event emit failed: kind=%s", kind)
