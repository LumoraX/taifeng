"""``EnginePool`` 的附件存储装配（attachment-store，ADR 0148）。

从 ``pool.py`` 拆出（该文件已近 800 行上限）：校验装配组合、构造每个 pool 一个的解析器。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.loop.attachment_resolve import AttachmentResolver

if TYPE_CHECKING:
    from taifeng.conversation.attachment_store import AttachmentStore
    from taifeng.loop.audit_config import AuditConfig


def build_attachment_resolver(
    store: AttachmentStore | None, *, audit: AuditConfig | None, cache_bytes: int,
) -> AttachmentResolver | None:
    """按装配参数构造解析器；未注入存储返回 None（行为与引入本能力之前一致）。

    Raises:
        ValueError: 同时配置了严格审计与附件存储（审计记录的引用形态单独立项，
            见 openspec ``attachment-store-journal``）；或 ``cache_bytes`` 为负。
    """
    if store is None:
        return None
    if audit is not None:
        raise ValueError(
            "attachment_store is not supported together with audit= yet "
            "(journal records still carry inline attachment bytes)"
        )
    return AttachmentResolver(store, cache_bytes=cache_bytes)


__all__ = ["build_attachment_resolver"]
