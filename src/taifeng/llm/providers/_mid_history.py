"""历史中段 system 消息 → 带标签 user 文本（mid-history-system，ADR 0055）。

供只有顶层 system 字段的原生 provider（Anthropic / Gemini）使用；OpenAI 系原生支持中段
system，不经此改写。
"""

from __future__ import annotations

from taifeng.llm.types import PartContent, TextPart

# 历史中段 system 消息改写为 user 消息时的包裹标签（mid-history-system）
_MID_SYSTEM_OPEN = "<system-reminder>"
_MID_SYSTEM_CLOSE = "</system-reminder>"


def mid_history_system_text(content: PartContent) -> str:
    """把历史中段的 system 消息改写为可放进 user 消息的文本。

    Anthropic / Gemini 原生 API 只有一个顶层 system 字段，messages 里不接受 system 角色。
    内核却会在历史中段放 system 消息：压缩摘要、压缩后 pinned 状态重注、预算提示、
    长期记忆预取、业务 ``InjectSystemMessage``。直接丢弃等于压缩后把被压缩的历史整段
    删掉；并进顶层 system 又会让每次注入都改写 cache 前缀。这里改写为带标签包裹的 user
    文本：保留在原位置（不破坏前缀缓存），标签让模型区分「系统注记」与用户原话。
    参照 claw-code ``compact.rs`` 以 user 角色承载压缩摘要。

    Args:
        content: ``ApiMessage.content``（文本或 part 列表，只取文本部分）。
    """
    if isinstance(content, str):
        text = content
    else:
        # system 注记只承载文本；part 列表里的图片不属于注记语义，只取文本部分
        text = "\n".join(
            part.text for part in content if isinstance(part, TextPart) and part.text
        )
    return f"{_MID_SYSTEM_OPEN}\n{text}\n{_MID_SYSTEM_CLOSE}"
