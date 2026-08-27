"""构建不包含 provider 密文与附件正文的摘要模型专用视图。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng.context.truncate import truncate_middle

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem


@dataclass(frozen=True, slots=True)
class _ViewItem:
    """仅包含允许进入 compaction prompt 的可见文本。"""

    label: str
    text: str


@dataclass(frozen=True, slots=True)
class CompactionView:
    """摘要模型唯一可消费的脱敏历史投影。"""

    items: tuple[_ViewItem, ...]

    @classmethod
    def from_items(cls, items: list[ResponseItem]) -> CompactionView:
        """按 item kind 白名单投影；未知 payload 不做字符串化。"""
        projected: list[_ViewItem] = []
        for item in items:
            if item.kind == "user_message":
                projected.append(_ViewItem("用户", str(item.payload.get("text", ""))))
            elif item.kind == "assistant_message":
                projected.append(_ViewItem("助手", str(item.payload.get("text", ""))))
            elif item.kind == "reasoning":
                text = str(item.payload.get("text", ""))
                summary = str(item.payload.get("summary", ""))
                projected.append(_ViewItem("推理", "\n".join(filter(None, (text, summary)))))
            elif item.kind == "function_call":
                projected.append(
                    _ViewItem(
                        "工具调用",
                        f"调用 {item.payload.get('name')}(call_id={item.payload.get('call_id')}): "
                        f"{item.payload.get('arguments', '')}",
                    )
                )
            elif item.kind == "function_call_output":
                output = truncate_middle(str(item.payload.get("output", "")), 1500)
                projected.append(
                    _ViewItem(
                        "工具结果",
                        f"call_id={item.payload.get('call_id')}: {output}",
                    )
                )
            elif item.kind == "system_injection":
                projected.append(_ViewItem("系统注入", str(item.payload.get("text", ""))))
            elif item.kind == "compacted":
                projected.append(_ViewItem("（已压缩摘要）", str(item.payload.get("summary", ""))))
            else:
                projected.append(_ViewItem(str(item.kind), "[内容未进入压缩视图]"))
        return cls(items=tuple(projected))

    def format_for_summary(self) -> str:
        """生成稳定的人类可读摘要输入，不访问原始 payload/metadata。"""
        lines: list[str] = []
        for index, item in enumerate(self.items, start=1):
            lines.extend((f"--- [{index}] {item.label} ---", item.text, ""))
        return "\n".join(lines)
