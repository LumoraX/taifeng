"""ToolRegistry —— 工具注册与查找。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from taifeng.llm.types import ToolSpecRef
from taifeng.tool.spec import ToolSpec


class DuplicateToolError(ValueError):
    pass


class UnknownToolError(KeyError):
    pass


class ToolRegistry:
    """工具注册表 —— 名称 → ToolSpec。"""

    def __init__(self, tools: Iterable[ToolSpec] = ()) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for t in tools:
            self.register(t)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise DuplicateToolError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

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
        return iter(self._tools.values())

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
