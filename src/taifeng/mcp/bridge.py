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
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from taifeng.mcp.content import McpContentError, convert_tool_result
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
    """从 MCP tools/call 结果提取纯文本投影 + is_error（不产出附件）。

    与桥内 handler 共用 ``convert_tool_result`` 的文本规则：图片为带 MIME / 字节数的显式
    占位（等同 ``attach_images=False``），resource 内联 text、blob 给占位，structuredContent
    缺等价 text 块时补序列化 JSON。需要图片附件与 structuredContent 原对象请直接用
    ``taifeng.mcp.content.convert_tool_result``。

    Raises:
        McpContentError: 结果形状不合法（见 ``convert_tool_result``）。
    """
    output = convert_tool_result(result, attach_images=False)
    return output.text, output.is_error


def _make_handler(client: McpClient, mcp_name: str, config: _BridgeConfig) -> Any:
    """构造调用远端工具的 handler（闭包绑定远端工具名）。

    结果投影见 ``convert_tool_result``：图片按 ``config.attach_images`` 进附件或占位；
    ``structuredContent`` 原对象进 ``data["structured_content"]``（保留 ``mcp_tool`` 键）。
    结果形状不合法 → 该次调用判错（``reason="mcp_invalid_content"``），不静默丢内容。
    """

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await asyncio.wait_for(
                client.call_tool(mcp_name, args), timeout=config.timeout_seconds)
        except McpToolError as e:
            return ToolResult.error(f"mcp_error: {e}", reason="mcp_error", code=e.code)
        except TimeoutError:
            return ToolResult.error("mcp_timeout", reason="timeout")
        try:
            output = convert_tool_result(result, attach_images=config.attach_images)
        except McpContentError as e:
            return ToolResult.error(
                f"mcp_invalid_content: {e}", reason="mcp_invalid_content", mcp_tool=mcp_name)
        data: dict[str, Any] = {"mcp_tool": mcp_name}
        if output.structured_content is not None:
            data["structured_content"] = output.structured_content
        return ToolResult(output=output.text, is_error=output.is_error, data=data,
                          attachments=output.attachments)

    return handler


@dataclass(frozen=True)
class _BridgeConfig:
    """一次绑定的命名与执行参数。"""

    tool_prefix: str
    parallel_safe: bool
    timeout_seconds: float
    trust_annotations: bool = False
    attach_images: bool = False
    """True → MCP image 块转 ``ToolResult.attachments``（宿主须启用 ``ImageInputPolicy``，
    否则按 tool-image-attachment 契约该次调用判错）；False（默认）→ 显式占位文本。"""


# 未信任 annotations 时的分类：远端工具一律假设有不可逆外部效果（崩溃恢复交人裁决）
_UNTRUSTED_EFFECT = ("external_non_idempotent", "manual")


def _classify_effect(meta: dict[str, Any], config: _BridgeConfig) -> tuple[str, str, bool]:
    """据 MCP tool annotations 推出 ``(effect_kind, reconciliation, parallel_safe)``。

    MCP 规范明言 annotations 只是提示、不可信 server 的提示不得据以决策；故默认
    （``trust_annotations=False``）不读，一律按外部不可幂等处理——以前这里落 ToolSpec
    默认的 ``pure``，崩溃恢复会告诉模型「可安全重发」，对写操作是错误的。

    业务信任该 server 时（``trust_annotations=True``）按规范语义细分，只认显式 ``true``：
    ``readOnlyHint`` → pure（且可与其他读类并行）；``idempotentHint`` → idempotent（可重发）；
    其余（含未声明，规范默认 readOnly=false / idempotent=false）→ 外部不可幂等。
    """
    if not config.trust_annotations:
        return (*_UNTRUSTED_EFFECT, config.parallel_safe)
    annotations = meta.get("annotations")
    if annotations is None:
        return (*_UNTRUSTED_EFFECT, config.parallel_safe)
    if not isinstance(annotations, dict):
        # 形状不对的提示按「未声明」处理并告警：保守分类本就是规范缺省值
        logger.warning("mcp tool %s has non-object annotations; treated as absent", meta.get("name"))
        return (*_UNTRUSTED_EFFECT, config.parallel_safe)
    if annotations.get("readOnlyHint") is True:
        return "pure", "none", True
    if annotations.get("idempotentHint") is True:
        return "idempotent", "retry", config.parallel_safe
    return (*_UNTRUSTED_EFFECT, config.parallel_safe)


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
        effect_kind, reconciliation, parallel_safe = _classify_effect(meta, config)
        specs[local_name] = ToolSpec(
            name=local_name,
            description=f"[MCP] {meta.get('description', '')}",
            input_schema=meta.get("inputSchema") or {"type": "object"},
            handler=_make_handler(client, name, config),
            parallel_safe=parallel_safe,
            effect_kind=effect_kind,
            reconciliation=reconciliation,
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
                    # annotations 变化（如 readOnly → 可写）同样要替换：分类决定恢复与并发语义
                    if current is not None and (
                        current.description != spec.description
                        or current.input_schema != spec.input_schema
                        or current.effect_kind != spec.effect_kind
                        or current.parallel_safe != spec.parallel_safe
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
    trust_annotations: bool = False,
    attach_images: bool = False,
) -> McpToolBinding:
    """注册 MCP server 的全部工具，并（默认）随 ``tools/list_changed`` 持续同步。

    Args:
        client: 已 initialize 的 MCP 客户端（stdio / HTTP）。
        registry: 目标注册表（通常是 pool 共享的那张）。
        tool_prefix: 本地命名前缀（避免与本地工具冲突，如 ``"mcp_fs_"``）。
        parallel_safe: 默认 False（MCP 工具通常有副作用）。
        timeout_seconds: 单次 tools/call 超时。
        watch: True → 登记 list_changed 监听，变更时自动重新同步。
        trust_annotations: True → 按 server 声明的 ``readOnlyHint`` / ``idempotentHint``
            细分副作用类型（只读工具另可并行）；默认 False 一律按外部不可幂等处理
            （MCP 规范：不可信 server 的提示不得据以决策）。
        attach_images: False（默认）→ 图片降级为带 MIME 与字节数的显式占位文本（模型知道有图、
            看不到内容）；True → 转为图片附件，走 tool-image-attachment 契约——宿主须已启用
            ``ImageInputPolicy``，否则该次调用以 ``tool_attachment_rejected`` 判错。默认不附图：
            图片策略默认关闭，默认附图会让所有返回截图的 MCP 工具整次调用失败（文本也丢）。
    """
    binding = McpToolBinding(
        client=client, registry=registry,
        config=_BridgeConfig(tool_prefix, parallel_safe, timeout_seconds, trust_annotations,
                             attach_images))
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
    trust_annotations: bool = False,
    attach_images: bool = False,
) -> list[ToolSpec]:
    """一次性把 MCP server 的所有工具注册为 ToolSpec（不随 list_changed 同步）。

    需要持续同步请用 ``bind_mcp_tools``；``trust_annotations`` / ``attach_images`` 语义同该函数。
    """
    binding = await bind_mcp_tools(
        client, registry, tool_prefix=tool_prefix, parallel_safe=parallel_safe,
        timeout_seconds=timeout_seconds, watch=False, trust_annotations=trust_annotations,
        attach_images=attach_images)
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
