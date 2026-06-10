"""build_prompt —— 把 (entry_skill, history, tool_specs) 装配成 ApiRequest。

参照：codex codex-rs/core/src/prompt.rs
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


def _item_to_api_message(it: ResponseItem) -> ApiMessage | None:
    """非采样产出的单条 ResponseItem → ApiMessage;记账类 kind 返回 None。

    assistant_message / function_call / function_call_output 在
    ``history_to_api_messages`` 主循环内特殊处理(同轮合并),不走本函数。
    """
    if it.kind == "user_message":
        return ApiMessage(role="user", content=str(it.payload.get("text", "")))
    if it.kind == "system_injection":
        # suspend_resolved 是 resume 的幂等记账 marker（engine._find_active_suspension
        # 据它跳过已消费的挂起），非 LLM-facing；若渲染成对话中段的 role="system"，
        # openai_compat 会原样透传 → 严格 OpenAI-compat 代理拒绝中段 system → 400
        # （anthropic/gemini provider 各自特判丢弃/转 user，openai_compat 不处理）。故跳过。
        # 业务/记忆类 system_injection（business / memory_pre_evict / rollback 等）保留。
        if it.payload.get("source") == "suspend_resolved":
            return None
        return ApiMessage(role="system", content=str(it.payload.get("text", "")))
    if it.kind == "compacted":
        # 把摘要作为 system 消息插回 —— LLM 视角看到"曾被压缩的历史"
        return ApiMessage(
            role="system",
            content=f"[Compacted history summary]\n{it.payload.get('summary', '')}",
        )
    # 其余 kind（suspension 等记账 item）不进 LLM 视图
    return None


def _fc_to_tool_call(it: ResponseItem) -> dict[str, Any]:
    """function_call item → OpenAI 形态的 tool_call dict。"""
    return {
        "id": it.payload.get("call_id", ""),
        "type": "function",
        "function": {
            "name": it.payload.get("name", ""),
            "arguments": it.payload.get("arguments", "{}"),
        },
    }


def history_to_api_messages(
    items: Iterable[ResponseItem],
    *,
    include_reasoning: bool = True,
) -> list[ApiMessage]:
    """把 ResponseItem 序列转 ApiMessage 序列(同轮合并重建)。

    **同轮合并**:一次采样的产出(assistant 文本 + 全部 function_call)落史时是
    多条 item(assistant_message → fc/fco 配对交错),重建时归并回**一条**
    assistant ApiMessage(content + tool_calls 同条)——忠实还原 provider 原始
    响应的 wire 形态。thinking 模型(deepseek-v4 等)校验**每条**带 tool_calls
    的 assistant 消息必须回传 reasoning_content,拆多条形态无法满足(真实 key
    验证 400),合并是唯一干净解。

    include_reasoning(reasoning-content-passback 旋钮):开(默认)时把
    ``kind="reasoning"`` 的文本附到其后首条 assistant 消息(即该采样轮的合并
    消息)上;关时与历史行为一致(reasoning 丢弃)。规则是确定性契约:同一
    history 必然重建出同一消息序(R2 前缀稳定);压缩剪枝产生的孤儿 reasoning
    (其后首条产出消息非 assistant)确定性跳过。
    """
    out: list[ApiMessage] = []
    # 暂存待附着的 reasoning 文本;开窗(assistant 消息产出)时附上并清空
    pending_reasoning: str | None = None
    # 当前采样轮的 assistant 消息在 out 中的下标(合并窗口);
    # user/system/compacted 产出即关窗,fco 不关窗(同轮并行 fc 的配对序
    # 是 fc,fco,fc,fco 交错),记账类 item(suspension 等)跨过保窗
    window_idx: int | None = None
    for it in items:
        if it.kind == "reasoning":
            if include_reasoning:
                pending_reasoning = str(it.payload.get("text", "")) or None
            continue
        if it.kind == "assistant_message":
            out.append(ApiMessage(
                role="assistant",
                content=str(it.payload.get("text", "")),
                reasoning=pending_reasoning,
            ))
            window_idx = len(out) - 1
            pending_reasoning = None
            continue
        if it.kind == "function_call":
            tc = _fc_to_tool_call(it)
            if window_idx is not None:
                tgt = out[window_idx]
                tgt.tool_calls = [*(tgt.tool_calls or []), tc]
            else:
                # 无前导 assistant 的孤立 fc(剪枝后的旧数据):独立成消息,
                # 自身即本轮的合并窗口(后续同轮 fc 仍归并到它)
                out.append(ApiMessage(
                    role="assistant", content="", tool_calls=[tc],
                    reasoning=pending_reasoning,
                ))
                window_idx = len(out) - 1
                pending_reasoning = None
            continue
        if it.kind == "function_call_output":
            out.append(ApiMessage(
                role="tool",
                content=str(it.payload.get("output", "")),
                tool_call_id=str(it.payload.get("call_id", "")),
            ))
            continue
        msg = _item_to_api_message(it)
        if msg is None:
            # 记账类 item 对 LLM 视图不存在,跨过它们保窗保 pending(确定性)
            continue
        # user/system/compacted 产出 = 新对话段:关窗 + 孤儿 reasoning 结算丢弃
        window_idx = None
        pending_reasoning = None
        out.append(msg)
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
    reasoning_passback: bool = True,
) -> ApiRequest:
    system_prompt = render_system_prompt(
        entry, snapshot, instructions=instructions, capabilities=capabilities
    )
    messages = history_to_api_messages(history, include_reasoning=reasoning_passback)

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
