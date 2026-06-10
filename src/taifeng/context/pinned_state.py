"""压缩后状态重注入(postcompact state re-injection)—— 协议 + 注册表。

参照 hermes ``conversation_compression.py`` 压缩后重注入 ``todo_snapshot`` 的
范式(``TodoStore.format_for_injection()`` 渲染 + MAX_* 上限防反噬)。差异:
  1. **协议化**:taifeng 不内置任何具体状态语义(todo 是业务范例),业务实现
     ``PinnedStateSource`` 注册进来,压缩成功后由 turn 层统一重注入 tail。
  2. **双层护栏**:per-source ``max_chars``(truncate_middle 截断)+ registry
     ``total_max_chars`` 总预算(装不下的 source 整体丢弃并如实记录),防止
     pinned 状态越积越大反噬压缩收益。
  3. **与 K3 正交**:MemoryStore 是「换出抢救」(swap-out salvage,异步 IO),
     本协议是「钉回保活」(纯内存渲染,同步)——两者叠加不合并。

渲染异常由 ``render_all`` 捕获进 ``errors`` 返回(调用方负责 emit 告警事件),
压缩主流程不被业务渲染炸掉;有事件、非 silent fallback。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from taifeng.context.truncate import truncate_middle

#: registry 总预算默认值(字符)——约束单轮压缩后 pinned 注入总增量有界。
DEFAULT_TOTAL_MAX_CHARS = 8000


@runtime_checkable
class PinnedStateSource(Protocol):
    """可在压缩后重注入的 agent-owned 状态源(业务实现)。

    同步协议:渲染应为纯内存格式化;需要 IO 的长期状态属 K3 ``MemoryStore``
    (async swap)职责——两协议的同步性差异本身就是职责边界的表达。
    """

    name: str
    """事件 / 审计标识,registry 内唯一。"""

    max_chars: int
    """单 source 渲染上限;超出由 ``truncate_middle`` 截断(保头尾)。"""

    def format_for_injection(self) -> str | None:
        """渲染当前状态为注入文本;返回 ``None`` 表示本次不注入。"""
        ...


@dataclass(frozen=True)
class PinnedRender:
    """单 source 的一次成功渲染结果(截断后)。"""

    name: str
    text: str


@dataclass(frozen=True)
class PinnedRenderResult:
    """``render_all`` 的聚合结果。

    Attributes:
        entries: 成功渲染(截断后、预算内)的条目,按注册序。
        dropped: 因总预算装不下而整体丢弃的 source 名(如实记录,不静默)。
        errors: 渲染抛异常的 ``(source_name, 异常描述)``,调用方负责告警。
    """

    entries: list[PinnedRender] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        """成功注入条目的字符总量(事件 data 用)。"""
        return sum(len(e.text) for e in self.entries)


class PinnedStateRegistry:
    """PinnedStateSource 注册表 —— 持有注册序与总预算,提供聚合渲染。

    总预算按**注册序**累计:先注册优先;业务对顺序敏感时自行控制注册序。
    """

    def __init__(self, total_max_chars: int = DEFAULT_TOTAL_MAX_CHARS) -> None:
        """Args:
            total_max_chars: 单轮注入字符总预算;累计超出的 source 整体丢弃。
        """
        self._total_max_chars = total_max_chars
        self._sources: dict[str, PinnedStateSource] = {}

    @property
    def total_max_chars(self) -> int:
        """单轮注入字符总预算。"""
        return self._total_max_chars

    def register(self, source: PinnedStateSource) -> None:
        """注册 source;同名已存在 → ``ValueError``(禁静默覆盖)。"""
        if source.name in self._sources:
            raise ValueError(
                f"pinned state source 同名已注册: {source.name!r}"
            )
        self._sources[source.name] = source

    def unregister(self, name: str) -> None:
        """注销 source;不存在 → ``KeyError``(显式失败,非 silent)。"""
        del self._sources[name]

    def __iter__(self):
        """按注册序迭代 source(dict 保序)。"""
        return iter(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def render_all(self) -> PinnedRenderResult:
        """按注册序渲染全部 source,应用双层护栏。

        流程(每 source):渲染 → ``None`` 跳过 → 异常捕获进 errors →
        per-source ``truncate_middle(max_chars)`` → 总预算累计判断,
        装不下整体丢弃进 dropped。

        Returns:
            PinnedRenderResult(entries / dropped / errors)。
        """
        entries: list[PinnedRender] = []
        dropped: list[str] = []
        errors: list[tuple[str, str]] = []
        used = 0
        for src in self._sources.values():
            try:
                raw = src.format_for_injection()
            except Exception as exc:  # 业务渲染崩溃不传染压缩主流程
                errors.append((src.name, f"{type(exc).__name__}: {exc}"))
                continue
            if raw is None:
                continue  # None = 本次不注入,语义上非丢弃
            text = truncate_middle(raw, src.max_chars)
            if used + len(text) > self._total_max_chars:
                dropped.append(src.name)
                continue
            used += len(text)
            entries.append(PinnedRender(name=src.name, text=text))
        return PinnedRenderResult(entries=entries, dropped=dropped, errors=errors)
