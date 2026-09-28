"""ToolRegistry —— 工具注册与查找，支持运行时增删与变更通知（dynamic-tool-set）。

工具集在运行期可以变化（MCP server 发 ``tools/list_changed``、业务侧热插拔工具）。
注册表负责三件事：

1. ``register`` / ``unregister`` / ``replace`` 原子地改名表；
2. 每次变更 ``version`` 单调 +1；
3. 同步通知已订阅的监听者（``ToolSetChange``）。EnginePool 订阅后在各 engine 上
   emit ``tool_set_changed`` 事件（R3）。

对运行中 turn 的语义：工具列表每次采样前按「声明层可见集 ∩ 注册表」重新计算，
所以变更在**下一次采样**生效；prompt 结构指纹随之变化，cache 失效被归因为
``tool_spec_changed``（预期内，不记 unexpected）。已发出、尚未派发的调用若指向被删
工具，派发层以 ``not_in_registry`` 错误结果核销。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from taifeng.llm.types import ToolSpecRef
    from taifeng.tool.spec import ToolSpec

logger = logging.getLogger(__name__)


class DuplicateToolError(ValueError):
    pass


class UnknownToolError(KeyError):
    pass


@dataclass(frozen=True)
class ToolSetChange:
    """一次工具集变更的事实。

    Attributes:
        added: 新增的工具名。
        removed: 移除的工具名。
        replaced: 同名替换（描述 / schema / handler 变化）的工具名。
        version: 变更后的注册表版本号。
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    version: int = 0

    def as_dict(self) -> dict[str, object]:
        """JSON 友好视图（事件 data 用）。"""
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "replaced": list(self.replaced),
            "version": self.version,
        }


class ToolRegistry:
    """工具注册表 —— 名称 → ToolSpec。"""

    def __init__(self, tools: Iterable[ToolSpec] = ()) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._version = 0
        self._listeners: list[Callable[[ToolSetChange], None]] = []
        for t in tools:
            self.register(t)

    @property
    def version(self) -> int:
        """注册表版本号：每次 register / unregister / replace 单调 +1。"""
        return self._version

    def subscribe(self, listener: Callable[[ToolSetChange], None]) -> Callable[[], None]:
        """订阅工具集变更；返回退订函数。

        监听者在变更**之后**同步调用；其异常被记日志并吞掉（一个坏监听者不得阻断
        工具集变更，也不得影响其余监听者）。
        """
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe

    def _notify(self, change: ToolSetChange) -> None:
        """版本 +1 后通知全部监听者。"""
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:
                logger.exception("tool set listener failed (ignored)")

    def register(self, spec: ToolSpec) -> None:
        """注册新工具。

        Raises:
            DuplicateToolError: 同名工具已存在（要替换请用 ``replace``）。
        """
        if spec.name in self._tools:
            raise DuplicateToolError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec
        self._version += 1
        self._notify(ToolSetChange(added=(spec.name,), version=self._version))

    def unregister(self, name: str) -> ToolSpec:
        """移除工具，返回被移除的 spec。

        Raises:
            UnknownToolError: 该工具未注册（不静默忽略——调用方以为删掉了其实没有）。
        """
        try:
            spec = self._tools.pop(name)
        except KeyError as e:
            raise UnknownToolError(name) from e
        self._version += 1
        self._notify(ToolSetChange(removed=(name,), version=self._version))
        return spec

    def replace(self, spec: ToolSpec) -> None:
        """同名替换已注册工具（描述 / schema / handler 更新）。

        Raises:
            UnknownToolError: 该工具未注册（新增请用 ``register``）。
        """
        if spec.name not in self._tools:
            raise UnknownToolError(spec.name)
        self._tools[spec.name] = spec
        self._version += 1
        self._notify(ToolSetChange(replaced=(spec.name,), version=self._version))

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as e:
            raise UnknownToolError(name) from e

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(list(self._tools.values()))

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools.keys())

    def to_specs_ref(self, allow: frozenset[str] | None = None) -> list[ToolSpecRef]:
        """生成 LLM 可见的 schema 列表，可按白名单过滤。"""
        return [
            t.to_ref()
            for t in self._tools.values()
            if allow is None or t.name in allow
        ]


__all__ = [
    "DuplicateToolError",
    "ToolRegistry",
    "ToolSetChange",
    "UnknownToolError",
]
