"""build_prompt —— 把 (entry_skill, history, tool_specs) 装配成 ApiRequest。

参照：codex codex-rs/core/src/prompt.rs
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.llm.types import ApiMessage, ApiRequest, CacheBreakpoint

if TYPE_CHECKING:
    from collections.abc import Iterable

    from taifeng.conversation.models import ResponseItem
    from taifeng.instructions.types import ResolvedInstruction
    from taifeng.llm.types import ToolSpecRef
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.eligibility import RuntimeCapabilities
    from taifeng.skill.registry import SkillSnapshot

SKILLS_INSTRUCTIONS_HEADER = """<entry_skill id="{id}" name="{name}">
{body}
</entry_skill>

<available_child_skills>
You can invoke these child skills via `call_skill(skill_id, args)`:

{child_lines}
</available_child_skills>

<dispatch_policy>
- 仅可调用上面 ``available_child_skills`` 列出的 skill
- 递归深度上限：{max_depth}
- 禁止循环调用（环检测会自动拒绝）
- 不可调用其他 entry skill
</dispatch_policy>"""


def _render_instructions_block(instructions: list[ResolvedInstruction]) -> str:
    """把 ResolvedInstruction 列表渲染为 XML 块串联，每层一个独立块。

    返回字符串末尾带换行，便于直接 prepend 到 SKILLS_INSTRUCTIONS_HEADER 前；
    若 instructions 为空，返回空串（保持向后兼容）。

    spec Requirement: 按 priority 升序拼接，每块含 ``name / scope / priority`` 属性。
    """
    if not instructions:
        return ""
    parts: list[str] = []
    for item in instructions:
        # 属性按固定顺序：priority / name / scope
        parts.append(
            f'<system_instructions name="{item.name}" scope="{item.scope}">\n'
            f"{item.text}\n"
            f"</system_instructions>"
        )
    return "\n\n".join(parts) + "\n\n"


def render_system_prompt(
    entry: SkillDefinition,
    snapshot: SkillSnapshot,
    instructions: list[ResolvedInstruction] | None = None,
    capabilities: RuntimeCapabilities | None = None,
) -> str:
    """生成入口 system prompt。

    - 若 ``instructions`` 非空，先按 priority 升序输出多个 ``<system_instructions>`` 块
    - 之后是 entry skill body 完整注入
    - 子 skill 列表（仅 id + description）注入；G4 过滤：``model_invocable=False``
      或（提供 ``capabilities`` 时）``requires`` 不满足的子 skill 不进列表
    - 不注入子 skill body（由 LLM 通过 ``read_skill`` 按需取）

    spec Requirement: 装配顺序 = system_instructions → entry_skill →
    available_child_skills → dispatch_policy。``instructions=None`` 或空时
    SHALL 不出现 ``<system_instructions>`` 子串（向后兼容）。
    """
    from taifeng.skill.eligibility import is_skill_eligible

    child_lines: list[str] = []
    for child_id in sorted(entry.child_skills):
        child = snapshot.get(child_id)
        if child is None:
            continue
        # G4b：对模型隐藏的 skill 不进列表
        if not child.exposure.model_invocable:
            continue
        # G4a：提供运行时能力快照时，过滤不满足 requires 的 skill
        if capabilities is not None and not is_skill_eligible(child, capabilities):
            continue
        child_lines.append(f"- `{child.id}`: {child.description}")
    body = SKILLS_INSTRUCTIONS_HEADER.format(
        id=entry.id,
        name=entry.name,
        body=entry.body,
        child_lines="\n".join(child_lines) if child_lines else "  (none)",
        max_depth=entry.max_call_depth,
    )
    if instructions:
        # 注：resolver.resolve 已经按 priority 升序返回；这里不重排避免破坏调用方
        # 的稳定语义（spec: 稳定排序，priority 相等保持 layers 出现顺序）。
        return _render_instructions_block(instructions) + body
    return body


def history_to_api_messages(items: Iterable[ResponseItem]) -> list[ApiMessage]:
    """把 ResponseItem 序列转 ApiMessage 序列。"""
    out: list[ApiMessage] = []
    for it in items:
        if it.kind == "user_message":
            out.append(ApiMessage(role="user", content=str(it.payload.get("text", ""))))
        elif it.kind == "assistant_message":
            out.append(ApiMessage(role="assistant", content=str(it.payload.get("text", ""))))
        elif it.kind == "function_call":
            out.append(
                ApiMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": it.payload.get("call_id", ""),
                            "type": "function",
                            "function": {
                                "name": it.payload.get("name", ""),
                                "arguments": it.payload.get("arguments", "{}"),
                            },
                        }
                    ],
                )
            )
        elif it.kind == "function_call_output":
            out.append(
                ApiMessage(
                    role="tool",
                    content=str(it.payload.get("output", "")),
                    tool_call_id=str(it.payload.get("call_id", "")),
                )
            )
        elif it.kind == "system_injection":
            # suspend_resolved 是 resume 的幂等记账 marker（engine._find_active_suspension
            # 据它跳过已消费的挂起），非 LLM-facing；若渲染成对话中段的 role="system"，
            # openai_compat 会原样透传 → 严格 OpenAI-compat 代理拒绝中段 system → 400
            # （anthropic/gemini provider 各自特判丢弃/转 user，openai_compat 不处理）。故跳过。
            # 业务/记忆类 system_injection（business / memory_pre_evict / rollback 等）保留。
            if it.payload.get("source") == "suspend_resolved":
                continue
            out.append(ApiMessage(role="system", content=str(it.payload.get("text", ""))))
        elif it.kind == "compacted":
            # 把摘要作为 system 消息插回 —— LLM 视角看到"曾被压缩的历史"
            out.append(
                ApiMessage(
                    role="system",
                    content=f"[Compacted history summary]\n{it.payload.get('summary', '')}",
                )
            )
        # reasoning 不进 LLM 视图（消耗 token 但不还原）
    return out


def build_api_request(
    *,
    entry: SkillDefinition,
    snapshot: SkillSnapshot,
    history: list[ResponseItem],
    tools: list[ToolSpecRef],
    model: str,
    cache_anchor_index: int = -1,
    instructions: list[ResolvedInstruction] | None = None,
    capabilities: RuntimeCapabilities | None = None,
    prefetched_memory: str | None = None,
) -> ApiRequest:
    system_prompt = render_system_prompt(
        entry, snapshot, instructions=instructions, capabilities=capabilities
    )
    messages = history_to_api_messages(history)

    # K3 prefetch（page-in）：把取回的长期记忆作为**尾部** system 消息注入，
    # 不动 system_prompt 头部（R2 cache-aware：变动的 prefetch 不破坏 cached 前缀）。
    if prefetched_memory:
        messages.append(ApiMessage(
            role="system",
            content=f"<retrieved_memory>\n{prefetched_memory}\n</retrieved_memory>",
        ))

    breakpoints: list[CacheBreakpoint] = []
    if cache_anchor_index >= 0:
        breakpoints.append(CacheBreakpoint(index=cache_anchor_index))

    return ApiRequest(
        model=model,
        system_prompt=[system_prompt],
        messages=messages,
        tools=tools,
        parallel_tool_calls=True,
        cache_breakpoints=breakpoints,
    )
