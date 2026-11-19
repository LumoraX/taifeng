"""附件存储事件（attachment-store，ADR 0148）：写入存储、取不回降级。

两个事件都不带正文，只带类型、大小、摘要前缀与引用（R3）。经 ``loop/event.py`` 原名再导出。
"""

from __future__ import annotations

from typing import Literal

from taifeng.loop.event_base import _Msg


class AttachmentStored(_Msg):
    """附件字节已写入附件存储，条目里将只落引用。

    data = {"origin": "user_message" | "tool_result", "kind": "image" | "file",
            "media_type": str, "size": int, "sha256": str（前 8 位）, "ref": str}
    """

    kind: Literal["attachment_stored"] = "attachment_stored"


class AttachmentUnavailable(_Msg):
    """历史条目上的附件取不回，本次请求以占位文本代替（turn 继续）。

    data = {"ref": str, "kind": str, "media_type": str, "item_id": str,
            "reason": "not_found" | "integrity_mismatch" | "backend_error"}
    """

    kind: Literal["attachment_unavailable"] = "attachment_unavailable"


__all__ = ["AttachmentStored", "AttachmentUnavailable"]
