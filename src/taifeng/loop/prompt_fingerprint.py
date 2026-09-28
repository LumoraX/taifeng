"""prompt 结构指纹 + 结构性 cache 失效归因（G-CACHE，ADR 0052）。

从 ``turn_sample.py`` 下沉的纯函数：指纹影响 cached prefix 的各段（参照 claw-code
``prompt_cache.rs`` 分段指纹）——可见 skill 列表 / tool 集合（名 + 描述 + schema）/
system 段（entry skill id + body + 注入指令文本）/ 模型 / 已发出消息前缀。history 在尾部
增长属正常，前缀段只记「发出时长度 + 这段的 item id 哈希」，下一轮只比较同一长度的那一段。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from taifeng.loop.turn_helpers import _sha1_short

if TYPE_CHECKING:
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.registry import SkillSnapshot


def history_prefix_hash(history: list[Any], length: int) -> str:
    """history 前 ``length`` 项的 id 序列哈希（cache 前缀改写检测用）。"""
    return _sha1_short(",".join(item.id for item in history[:length]))


def compute_prompt_fingerprint(
    *,
    snapshot: SkillSnapshot,
    entry_skill: SkillDefinition,
    instructions: list[Any],
    history: list[Any],
    tools: list[Any],
) -> dict[str, str]:
    """计算本轮 prompt 的分段结构指纹。"""
    snapshot_key = ",".join(sorted(snapshot.reachable_from(entry_skill.id)))
    # 工具指纹含描述与 schema：同名替换（ToolRegistry.replace / MCP list_changed）
    # 同样改变 cached prefix，只比名字会把这类失效误记为 unknown_drop
    tools_key = ",".join(sorted(
        f"{getattr(t, 'name', '')}:{getattr(t, 'description', '')}:"
        f"{json.dumps(getattr(t, 'input_schema', {}), sort_keys=True, ensure_ascii=False)}"
        for t in tools
    ))
    instr_text = "\x01".join(getattr(i, "text", "") for i in instructions)
    system_src = f"{entry_skill.id}\x00{entry_skill.body}\x00{instr_text}"
    return {
        "snapshot": _sha1_short(snapshot_key),
        "tools": _sha1_short(tools_key),
        "system": _sha1_short(system_src),
        "model": _sha1_short(entry_skill.model or ""),
        "prefix_len": str(len(history)),
        "prefix": history_prefix_hash(history, len(history)),
    }


def detect_structural_break_reason(
    prev: dict[str, str] | None, current: dict[str, str], history: list[Any],
) -> str | None:
    """对比上一轮指纹，判定本轮 cache 失效的结构性原因（无变更 → None）。"""
    if prev is None:
        return None
    if current.get("snapshot") != prev.get("snapshot"):
        return "skill_snapshot_changed"
    if current.get("tools") != prev.get("tools"):
        return "tool_spec_changed"
    if current.get("system") != prev.get("system"):
        return "system_prompt_changed"
    if "model" in prev and current.get("model") != prev.get("model"):
        return "model_changed"
    # 已缓存前缀：上次发出的那段 history（按长度截取）id 序列是否仍一致。
    # 缩短（rollback）或同长度内被替换都算改写；压缩 / rewind 已在更早的预期标记里归因。
    prev_len = int(prev.get("prefix_len", "0"))
    if prev.get("prefix") is not None and (
        len(history) < prev_len or history_prefix_hash(history, prev_len) != prev.get("prefix")
    ):
        return "message_prefix_changed"
    return None
