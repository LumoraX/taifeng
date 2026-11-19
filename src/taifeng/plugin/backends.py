"""插件提供存储后端（plugin-backend-providers，两段加载的第一段）。

pool 在构造 store 之前就要知道用哪个后端，而 ``setup(ctx)`` 在 pool 构造之后才运行。所以后端走单独
的、更早的一段：插件可选实现同步方法 ``provide_backends(backends)``，只**声明**工厂、不做 IO；
pool 在建 store 之前收集并调用工厂。

规则：每个后端 capability 每个 pool 至多一个提供者；与宿主显式参数或其他插件冲突一律报错，
不静默覆盖、不静默忽略。声明阶段结束即冻结，运行中不可替换持久化真相源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from taifeng.plugin.errors import PluginCapabilityError
from taifeng.plugin.manager import resolve_load_order

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from taifeng.conversation.attachment_store import AttachmentStore
    from taifeng.conversation.protocols import ThreadDirectory
    from taifeng.conversation.store import MessageStore
    from taifeng.loop.audit_config import AuditConfig
    from taifeng.plugin.types import Plugin

BackendKind = Literal["message_store", "thread_directory", "audit", "attachment_store"]
"""可由插件提供的后端。``audit`` 提供整份 ``AuditConfig``（journal core 离不开 writer_id 与附件上限）；
``attachment_store`` 提供附件字节的独立存储（ADR 0148）。"""


@dataclass(frozen=True, slots=True)
class BackendProvider:
    """一个已声明的后端提供者。

    Attributes:
        plugin_id: 提供者插件 id。
        factory: 无参工厂；pool 在构造 store 前调用一次。
    """

    plugin_id: str
    factory: Callable[[], object]


class PluginBackends:
    """``provide_backends`` 阶段交给插件的受限登记器。"""

    __slots__ = ("_plugin_id", "_sealed", "_table")

    def __init__(self, plugin_id: str, table: dict[BackendKind, BackendProvider]) -> None:
        """由收集函数构造。"""
        self._plugin_id = plugin_id
        self._table = table
        self._sealed = False

    def message_store(self, factory: Callable[[], MessageStore]) -> None:
        """声明会话主存（等价于宿主传 ``message_store=``；pool 拥有并在关闭时关闭它）。"""
        self._declare("message_store", factory)

    def thread_directory(self, factory: Callable[[], ThreadDirectory]) -> None:
        """声明 thread 目录（等价于宿主传 ``thread_directory=``；pool 不关闭它）。"""
        self._declare("thread_directory", factory)

    def attachment_store(self, factory: Callable[[], AttachmentStore]) -> None:
        """声明附件存储（等价于宿主传 ``attachment_store=``；pool 不关闭它，ADR 0148）。"""
        self._declare("attachment_store", factory)

    def audit(self, factory: Callable[[], AuditConfig]) -> None:
        """声明审计配置（等价于宿主传 ``audit=``，含 journal core）。"""
        self._declare("audit", factory)

    def _declare(self, kind: BackendKind, factory: Callable[[], object]) -> None:
        if self._sealed:
            raise PluginCapabilityError(
                f"plugin {self._plugin_id!r} declared backend {kind!r} after provide_backends returned",
                plugin_id=self._plugin_id,
                capability=kind,
            )
        existing = self._table.get(kind)
        if existing is not None:
            raise PluginCapabilityError(
                f"backend {kind!r} provided by both {existing.plugin_id!r} and {self._plugin_id!r}",
                plugin_id=self._plugin_id,
                capability=kind,
            )
        self._table[kind] = BackendProvider(plugin_id=self._plugin_id, factory=factory)

    def _seal(self) -> None:
        self._sealed = True


def collect_backend_providers(plugins: Sequence[Plugin]) -> dict[BackendKind, BackendProvider]:
    """按加载顺序调用各插件可选的 ``provide_backends``，返回冻结前的提供者表。

    Raises:
        PluginCapabilityError: 两个插件提供同一后端，或封口后再声明。
        依赖图错误：见 ``resolve_load_order``（与 setup 阶段同一校验，在任何 IO 之前失败）。
    """
    table: dict[BackendKind, BackendProvider] = {}
    for plugin in resolve_load_order(plugins):
        provide = getattr(plugin, "provide_backends", None)
        if provide is None:
            continue
        backends = PluginBackends(plugin.spec.id, table)
        try:
            provide(backends)
        finally:
            backends._seal()  # noqa: SLF001
    return table


__all__ = ["BackendKind", "BackendProvider", "PluginBackends", "collect_backend_providers"]
