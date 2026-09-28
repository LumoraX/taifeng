"""handoff 压缩后的衔接（compaction-continuity）：最近用户原话 + 续接前言。

LLM 摘要最容易丢的是**用户的原始意图与约束**：「报告写三段」「不要改公共 API」这类措辞
一旦被转述，细节与语气就走样；被压缩区间里用户说过的话又不会再出现。压缩后的模型还常
先「确认收到摘要 / 复述进度」再开工，白耗一轮。

本模块把压缩条目的正文组装为三段，全部放在同一个 ``compacted`` item 的 ``summary`` 里：

1. 续接前言：告诉模型之前的历史已被压缩，直接接着干；
2. ``<recent_user_messages>``：被压缩区间里最近的用户消息原文（按原顺序，总量受 token 预算约束）；
3. ``<summary>``：LLM 生成的结构化摘要。

放在同一个 item 里而不是另插 user item：冷加载重建（``replaced_range`` 折叠）、token 估算、
各 provider 的中段 system 渲染都无需改动，且压缩结果仍是「一个区间 → 一个条目」。

参照：codex ``compact.rs``（``collect_user_messages`` + ``COMPACT_USER_MESSAGE_MAX_TOKENS``
= 20k，以及 summary 前缀提示）。差异：codex 把用户消息作为独立 user 条目放回历史；taifeng
合并进压缩条目，理由见上。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.context.budget import estimate_text_tokens
from taifeng.context.truncate import truncate_middle

if TYPE_CHECKING:
    from taifeng.conversation.models import ResponseItem

DEFAULT_PRESERVED_USER_TOKENS = 20_000
"""最近用户原话的默认 token 预算（与 codex 同值）。"""

# 与 estimate_text_tokens 同一经验比例（len / 3.5），用于把 token 预算换算成截断字符数
_CHARS_PER_TOKEN = 3.5

CONTINUATION_PREAMBLE = (
    "The conversation before this point was compacted to save context. Continue the "
    "work directly from the material below: do not acknowledge the summary, do not "
    "restate progress, and ask the user only if the pending work genuinely needs it."
)
"""续接前言（模型可见）。英文书写：与既有 ``[Compacted history summary]`` 标签一致，
且不带偏模型的回复语言——摘要本身按原对话语言生成。"""


def select_recent_user_messages(
    items: list[ResponseItem], budget_tokens: int,
) -> list[str]:
    """从被压缩区间里选出最近的用户消息原文（返回按原顺序排列）。

    从最新往回累加，超出预算即停；若最新一条单独就超预算，则中段截断到预算内
    （保头尾，意图与约束常在首尾）。``budget_tokens <= 0`` 返回空列表（关闭此功能）。
    """
    if budget_tokens <= 0:
        return []
    chosen: list[str] = []
    used = 0
    for item in reversed(items):
        if item.kind != "user_message":
            continue
        text = str(item.payload.get("text", ""))
        if not text.strip():
            continue
        cost = estimate_text_tokens(text)
        if used + cost > budget_tokens:
            if not chosen:
                # 最新一条就超预算：截断保留，而不是整条丢掉
                chosen.append(truncate_middle(text, int(budget_tokens * _CHARS_PER_TOKEN)))
            break
        chosen.append(text)
        used += cost
    chosen.reverse()
    return chosen


def compose_handoff_summary(summary: str, user_messages: list[str]) -> str:
    """组装压缩条目正文：续接前言 + 最近用户原话 + LLM 摘要。"""
    parts = [CONTINUATION_PREAMBLE]
    if user_messages:
        quoted = "\n".join(f"<user_message>\n{m}\n</user_message>" for m in user_messages)
        parts.append(
            "<recent_user_messages>\n"
            "Most recent user messages from the compacted span, verbatim, oldest first:\n"
            f"{quoted}\n</recent_user_messages>"
        )
    parts.append(f"<summary>\n{summary}\n</summary>")
    return "\n\n".join(parts)
