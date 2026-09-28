"""MCP → Taifeng 工具桥：把 MCP server 的工具注册为 ToolSpec，并随 ``list_changed`` 同步。

与传输无关：stdio（``McpStdioClient``）与 streamable HTTP（``McpHttpClient``）都实现
``McpClient`` 协议，共用本桥。

两种用法：

- ``register_mcp_tools_async``：一次性注册（旧接口，行为不变）；
- ``bind_mcp_tools``：注册并**持续同步**——server 发 ``notifications/tools/list_changed``
  时重新 ``tools/list``，按差异 ``register`` / ``unregister`` / ``replace``，注册表随即
  通知各 engine（``tool_set_changed``），下一次采样看到新工具集（dynamic-tool-set）。

参照：codex ``codex-rs/rmcp-client``（list_changed 重新拉取）；opencode ``mcp/index.ts``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from taifeng.tool.registry import ToolRegistry

logger = logging.getLogger(__name__)

class McpToolError(Exception):
    """MCP 工具调用失败（JSON-RPC error 或传输层错误）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class McpClient(Protocol):
    """桥所需的最小 MCP 客户端能力（stdio / HTTP 两种传输都实现）。"""

    async def list_tools(self) -> list[dict[str, Any]]:
        """``tools/list``：返回工具元数据列表。"""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """``tools/call``：执行远端工具，返回原始 result。"""
        ...

    @property
    def server_info(self) -> dict[str, Any]:
        """initialize 返回的 serverInfo。"""
        ...

    def add_tools_changed_listener(self, listener: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """登记 ``notifications/tools/list_changed`` 的异步回调。"""
        ...


def extract_text_content(result: dict[str, Any]) -> tuple[str, bool]:
    """从 MCP tools/call 结果提取文本 + is_error。"""
    is_error = bool(result.get("isError"))
    content = result.get("content") or []
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            parts.append(f"[image: {item.get('mimeType', 'unknown')}]")
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(parts), is_error


def _make_handler(client: McpClient, mcp_name: str, timeout_seconds: float) -> Any:
    """构造调用远端工具的 handler（闭包绑定远端工具名）。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await asyncio.wait_for(
                client.call_tool(mcp_name, args), timeout=timeout_seconds)
        except McpToolError as e:
            return ToolResult.error(f"mcp_error: {e}", reason="mcp_error", code=e.code)
        except TimeoutError:
            return ToolResult.error("mcp_timeout", reason="timeout")
        text, is_error = extract_text_content(result)
        return ToolResult(output=text, is_error=is_error, data={"mcp_tool": mcp_name})

    return handler


@dataclass(frozen=True)
class _BridgeConfig:
    """一次绑定的命名与执行参数。"""

    tool_prefix: str
    parallel_safe: bool
    timeout_seconds: float


def _specs_from_listing(
    client: McpClient, listing: list[dict[str, Any]], config: _BridgeConfig,
) -> dict[str, ToolSpec]:
    """把 tools/list 结果转成 {本地名: ToolSpec}；形状不对的条目跳过。"""
    specs: dict[str, ToolSpec] = {}
    for meta in listing:
        if not isinstance(meta, dict):
            continue
        name = meta.get("name")
        if not name or not isinstance(name, str):
            continue
        local_name = f"{config.tool_prefix}{name}"
        specs[local_name] = ToolSpec(
            name=local_name,
            description=f"[MCP] {meta.get('description', '')}",
            input_schema=meta.get("inputSchema") or {"type": "object"},
            handler=_make_handler(client, name, config.timeout_seconds),
            parallel_safe=config.parallel_safe,
            timeout_seconds=config.timeout_seconds + 5.0,
        )
    return specs


@dataclass
class McpToolBinding:
    """一个 MCP server 与注册表之间的持续绑定。

    Attributes:
        owned: 本绑定当前在注册表里拥有的本地工具名（只增删自己的，不碰别人的工具）。
    """

    client: McpClient
    registry: ToolRegistry
    config: _BridgeConfig
    owned: set[str] = field(default_factory=set)
    _sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def sync(self) -> tuple[list[str], list[str], list[str]]:
        """重新拉取工具列表并按差异更新注册表。

        Returns:
            (新增, 移除, 替换) 的本地工具名。

        同名工具只在描述或 schema 变化时替换；与非本绑定的已注册工具重名时跳过并告警
        （不抢占别人的工具）。串行化：并发的 list_changed 通知排队执行。
        """
        async with self._sync_lock:
            desired = _specs_from_listing(
                self.client, await self.client.list_tools(), self.config)
            added: list[str] = []
            removed: list[str] = []
            replaced: list[str] = []
            for name in sorted(self.owned - desired.keys()):
                self.registry.unregister(name)
                self.owned.discard(name)
                removed.append(name)
            for name, spec in desired.items():
                if name in self.owned:
                    current = self.registry.get(name)
                    if current is not None and (
                        current.description != spec.description
                        or current.input_schema != spec.input_schema
                    ):
                        self.registry.replace(spec)
                        replaced.append(name)
                    continue
                if name in self.registry:
                    logger.warning("mcp tool %s collides with an existing tool; skipped", name)
                    continue
                self.registry.register(spec)
                self.owned.add(name)
                added.append(name)
            return added, removed, replaced

    async def _on_list_changed(self) -> None:
        """list_changed 回调：同步失败只记日志（server 抖动不应打断宿主）。"""
        try:
            added, removed, replaced = await self.sync()
        except Exception:
            logger.exception("mcp tools re-sync failed for %s", self.client.server_info.get("name"))
            return
        logger.info("mcp tools re-synced: +%s -%s ~%s", added, removed, replaced)

    def detach(self) -> None:
        """从注册表移除本绑定拥有的全部工具（server 断开 / 业务卸载时调用）。"""
        for name in sorted(self.owned):
            self.registry.unregister(name)
        self.owned.clear()


async def bind_mcp_tools(
    client: McpClient,
    registry: ToolRegistry,
    *,
    tool_prefix: str = "",
    parallel_safe: bool = False,
    timeout_seconds: float = 60.0,
    watch: bool = True,
) -> McpToolBinding:
    """注册 MCP server 的全部工具，并（默认）随 ``tools/list_changed`` 持续同步。

    Args:
        client: 已 initialize 的 MCP 客户端（stdio / HTTP）。
        registry: 目标注册表（通常是 pool 共享的那张）。
        tool_prefix: 本地命名前缀（避免与本地工具冲突，如 ``"mcp_fs_"``）。
        parallel_safe: 默认 False（MCP 工具通常有副作用）。
        timeout_seconds: 单次 tools/call 超时。
        watch: True → 登记 list_changed 监听，变更时自动重新同步。
    """
    binding = McpToolBinding(
        client=client, registry=registry,
        config=_BridgeConfig(tool_prefix, parallel_safe, timeout_seconds))
    await binding.sync()
    if watch:
        client.add_tools_changed_listener(binding._on_list_changed)
    return binding


async def register_mcp_tools_async(
    client: McpClient,
    registry: ToolRegistry,
    *,
    tool_prefix: str = "",
    parallel_safe: bool = False,
    timeout_seconds: float = 60.0,
) -> list[ToolSpec]:
    """一次性把 MCP server 的所有工具注册为 ToolSpec（不随 list_changed 同步）。

    需要持续同步请用 ``bind_mcp_tools``。
    """
    binding = await bind_mcp_tools(
        client, registry, tool_prefix=tool_prefix, parallel_safe=parallel_safe,
        timeout_seconds=timeout_seconds, watch=False)
    registered = [spec for name in sorted(binding.owned)
                  if (spec := registry.get(name)) is not None]
    logger.info("registered %d MCP tool(s) from %s", len(registered), client.server_info.get("name"))
    return registered


def register_mcp_tools(
    client: McpClient,
    registry: ToolRegistry,
    *,
    tool_prefix: str = "",
    parallel_safe: bool = False,
    timeout_seconds: float = 60.0,
) -> list[ToolSpec]:
    """同步入口已废弃：tools/list 是网络调用，请改用 ``await register_mcp_tools_async``。

    Raises:
        RuntimeError: 恒抛（保留符号仅为给出明确迁移提示）。
    """
    raise RuntimeError("use `await register_mcp_tools_async(...)` instead")


__all__ = [
    "McpClient",
    "McpToolBinding",
    "McpToolError",
    "bind_mcp_tools",
    "extract_text_content",
    "register_mcp_tools",
    "register_mcp_tools_async",
]
