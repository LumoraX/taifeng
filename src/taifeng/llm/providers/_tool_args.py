"""历史 tool call 参数的回放解析 —— Anthropic / Gemini 共用（ADR 0074）。

OpenAI 系协议把 tool call 参数当**字符串**回放，模型当初写坏的 JSON 原样送回即可。
Anthropic ``tool_use.input`` 与 Gemini ``functionCall.args`` 要求 **JSON 对象**，
写坏的参数无法原样表达。

此前两家在解析失败时把参数静默改成 ``{}``：模型在历史里看到的是「自己发过一次无参调用」，
与紧随其后的 ``invalid_arguments`` 错误结果对不上，既无从知道当初写错了什么，也违反
「禁止静默回退」。非对象 JSON（list / str / number / null）则原样穿透，被 provider 以 400 拒绝。

本模块的处理：解析失败时回放一个**显式标记对象**，保留错误分类与原始文本，并记 warning。
不抛异常——历史不可改写，抛错会让该会话此后的每一次请求都失败。

与 ``loop/tool_batch.parse_tool_arguments`` 的分工：那边是**派发前裁决**（坏参数不执行
handler），这边是**回放时表达**（坏参数如实送回模型）。错误分类的文案两边保持一致，
但 llm 层不依赖 loop 层，故各自实现。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 标记对象的两个键：错误分类 + 模型当初产出的原始文本
INVALID_ARGUMENTS_KEY = "__invalid_arguments__"
RAW_ARGUMENTS_KEY = "__raw_arguments__"

# 原始文本保留上限（字符）：写坏的参数可能是被截断的超长输出，不能无上限回灌上下文
DEFAULT_MAX_RAW_CHARS = 4000


def _truncate_raw(raw: str, max_raw_chars: int) -> str:
    """超长原始文本截前缀并注明原长度；截断结果只由输入决定（保 cache 前缀稳定）。"""
    if len(raw) <= max_raw_chars:
        return raw
    return f"{raw[:max_raw_chars]}…[truncated, original {len(raw)} chars]"


def _invalid_marker(
    error: str, raw: str, *, tool_name: str, max_raw_chars: int,
) -> dict[str, Any]:
    """构造显式标记对象并记 warning。"""
    logger.warning(
        "历史 tool call 参数不是合法 JSON 对象，按显式标记回放: tool=%s error=%s raw_chars=%d",
        tool_name,
        error,
        len(raw),
    )
    return {
        INVALID_ARGUMENTS_KEY: error,
        RAW_ARGUMENTS_KEY: _truncate_raw(raw, max_raw_chars),
    }


def replay_tool_arguments(
    raw: object,
    *,
    tool_name: str,
    max_raw_chars: int = DEFAULT_MAX_RAW_CHARS,
) -> dict[str, Any]:
    """把历史 tool call 的参数转成 provider 要求的 JSON 对象。

    规则：
    - 已是对象 → 浅拷贝返回；
    - 空串 / 全空白 → ``{}``（无参工具的合法空对象）；
    - 合法 JSON 对象字符串 → 解析结果；
    - 非法 JSON / 非对象 JSON / 其他类型 → 显式标记对象
      ``{INVALID_ARGUMENTS_KEY: <错误分类>, RAW_ARGUMENTS_KEY: <原始文本>}``。

    返回值恒为 ``dict``；同一输入恒得同一输出。
    """
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return _invalid_marker(
            f"not_an_object: got {type(raw).__name__}",
            json.dumps(raw, ensure_ascii=False, default=str),
            tool_name=tool_name,
            max_raw_chars=max_raw_chars,
        )
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _invalid_marker(
            f"invalid_json: {exc.msg} (pos {exc.pos})",
            raw,
            tool_name=tool_name,
            max_raw_chars=max_raw_chars,
        )
    if not isinstance(parsed, dict):
        return _invalid_marker(
            f"not_an_object: got {type(parsed).__name__}",
            raw,
            tool_name=tool_name,
            max_raw_chars=max_raw_chars,
        )
    return parsed
