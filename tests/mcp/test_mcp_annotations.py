"""mcp-annotations —— MCP 工具的副作用分类：默认保守，信任时按 annotations 细分。

回归点：桥接出的 ToolSpec 此前落默认 ``effect_kind="pure"``，崩溃恢复会对任何 MCP
写操作告诉模型「可安全重发」。
"""

from __future__ import annotations

from typing import Any

import pytest

from taifeng.mcp import bind_mcp_tools
from taifeng.tool.registry import ToolRegistry


class _FakeClient:
    """按给定 listing 返回 tools/list 的最小 MCP 客户端。"""

    def __init__(self, listing: list[dict[str, Any]]) -> None:
        self.listing = listing

    async def list_tools(self) -> list[dict[str, Any]]:
        return self.listing

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": name}]}

    @property
    def server_info(self) -> dict[str, Any]:
        return {"name": "fake"}

    def add_tools_changed_listener(self, listener: Any) -> None:
        return None


def _tool(name: str, annotations: Any = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "description": name, "inputSchema": {"type": "object"}}
    if annotations is not None:
        meta["annotations"] = annotations
    return meta


LISTING = [
    _tool("read", {"readOnlyHint": True}),
    _tool("upsert", {"readOnlyHint": False, "idempotentHint": True}),
    _tool("send", {"destructiveHint": True}),
    _tool("bare"),
    _tool("weird", "not-an-object"),
    _tool("stringly", {"readOnlyHint": "true"}),
]


def _classes(registry: ToolRegistry) -> dict[str, tuple[str, str, bool]]:
    return {name: (spec.effect_kind, spec.reconciliation, spec.parallel_safe)
            for name in sorted(registry.names()) if (spec := registry.get(name))}


async def test_untrusted_annotations_default_to_external_non_idempotent() -> None:
    """默认不信任 annotations：全部按外部不可幂等 + 人工核对，不再是 pure。"""
    registry = ToolRegistry()
    await bind_mcp_tools(_FakeClient(LISTING), registry, watch=False)
    assert set(_classes(registry).values()) == {("external_non_idempotent", "manual", False)}


async def test_trusted_annotations_refine_effect_kind() -> None:
    """信任时按规范语义细分；只认显式 true，缺省 / 畸形 / 字符串一律保守。"""
    registry = ToolRegistry()
    await bind_mcp_tools(_FakeClient(LISTING), registry, watch=False, trust_annotations=True)
    conservative = ("external_non_idempotent", "manual", False)
    assert _classes(registry) == {
        "read": ("pure", "none", True),
        "upsert": ("idempotent", "retry", False),
        "send": conservative,
        "bare": conservative,
        "weird": conservative,
        "stringly": conservative,
    }


async def test_annotation_change_on_resync_replaces_spec() -> None:
    """重新同步时 annotations 变化（只读 → 可写）会替换已注册工具。"""
    registry = ToolRegistry()
    client = _FakeClient([_tool("t", {"readOnlyHint": True})])
    binding = await bind_mcp_tools(client, registry, watch=False, trust_annotations=True)
    client.listing = [_tool("t", {"readOnlyHint": False})]
    added, removed, replaced = await binding.sync()
    assert (added, removed, replaced) == ([], [], ["t"])
    spec = registry.get("t")
    assert spec is not None and spec.effect_kind == "external_non_idempotent"


@pytest.mark.parametrize("trust", [False, True])
async def test_explicit_parallel_safe_is_kept(trust: bool) -> None:
    """业务显式 parallel_safe=True 不被分类降级。"""
    registry = ToolRegistry()
    await bind_mcp_tools(_FakeClient([_tool("bare")]), registry, watch=False,
                         parallel_safe=True, trust_annotations=trust)
    spec = registry.get("bare")
    assert spec is not None and spec.parallel_safe
