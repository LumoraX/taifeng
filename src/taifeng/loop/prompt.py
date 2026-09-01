"""build_prompt —— 把 (entry_skill, history, tool_specs) 装配成 ApiRequest。

参照：codex codex-rs/core/src/prompt.rs
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from taifeng.llm.client import TEXT_ONLY_CAPABILITIES, ModelCapabilities
from taifeng.llm.errors import InvalidHistoryError
from taifeng.llm.image_input import (
    DISABLED_IMAGE_POLICY,
    ImageAttachmentV1,
    ImageInputPolicy,
    admit_image_attachments,
)
from taifeng.llm.types import (
    ApiFunctionCallItem,
    ApiFunctionCallOutputItem,
    ApiInputItem,
    ApiMessage,
    ApiMessageItem,
    ApiProviderStateItem,
    ApiRequest,
    CacheBreakpoint,
    ImagePart,
    ProviderStateEnvelope,
    TextPart,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from taifeng.conversation.models import ResponseItem
    from taifeng.instructions.types import ResolvedInstruction
    from taifeng.llm.types import ToolSpecRef
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.eligibility import RuntimeCapabilities
    from taifeng.skill.registry import SkillSnapshot

# default 召回阈值（child 数 > 此值时 auto 模式切 deferred）。turn 层会用 pool
# 注入的 recall_threshold 覆盖；这里仅作 render_system_prompt 缺省参数兜底，保证
# 直接调用（如单测 / 老调用方）行为稳定（小白名单恒走 inline，向后兼容）。
DEFAULT_RECALL_THRESHOLD = 50

SKILLS_INSTRUCTIONS_HEADER = """<entry_skill id="{id}" name="{name}">
{body}
</entry_skill>

<available_child_skills>
{child_block}
</available_child_skills>

<dispatch_policy>
- 仅可调用上面 ``available_child_skills`` 列出的 skill
- 递归深度上限：{max_depth}
- 禁止循环调用（环检测会自动拒绝）
- 不可调用其他 entry skill
</dispatch_policy>"""

# inline 模式：逐一列出 child（id + description）供 LLM 直接 call_skill。
_INLINE_CHILD_BLOCK = """You can invoke these child skills via `call_skill(skill_id, args)`:

{child_lines}"""

# deferred 模式：child 太多、不在此逐一列出，改提示用 search_skills 按需召回。
# N=G4 过滤后的可见 child 数（让 LLM 知道池子有多大）。
# 提示词写法经真实 A/B 验证（docs 台账 + examples/real_llm/skill_select）：内核默认召回是
# 关键词匹配，故须显式引导 LLM 把口语意图转译成「关键词 query」并在没命中时改词重搜——
# 否则口语直喂会召不中。这是通用 prompt engineering（ReAct 循环），不含任何业务概念（R1）。
_DEFERRED_CHILD_BLOCK = """子 skill 较多（共 {child_count} 个），未逐一列出，需用 search_skills 主动发现。按以下循环：
1. 思考：当前子任务需要"什么能力"？列出能力关键词 + 同义词（多词覆盖近义说法）。
2. 行动：search_skills(query=这些关键词，不要照抄用户原话的口语复述)。
3. 观察+反思：读候选 description / confidence；若无贴切候选或 confidence 普遍偏低，换一组关键词再 search。
4. 选定后立即用 call_skill(skill_id, args) 派发，不要停在检索。"""


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
    *,
    recall_threshold: int = DEFAULT_RECALL_THRESHOLD,
    has_recall_backend: bool = False,
) -> str:
    """生成入口 system prompt（只管文本，不碰 per-turn tools 列表）。

    - 若 ``instructions`` 非空，先按 priority 升序输出多个 ``<system_instructions>`` 块
    - 之后是 entry skill body 完整注入
    - ``available_child_skills`` 块按生效召回模式（见
      ``skill.visibility.effective_child_recall``）二选一：
        * **inline**：逐一列子 skill（仅 id + description）供 LLM 直接 call_skill；
        * **deferred**：不列 child，改提示用 ``search_skills`` 按需召回后再 call_skill。
    - 两条分支共用 ``visible_child_skills`` 做 G4 过滤（``model_invocable=False`` 或
      ``requires`` 不满足的子 skill 不进列表 / 不计入召回池规模），与 deferred 召回池
      **同源同过滤**（消除双实现漂移）。
    - 不注入子 skill body（由 LLM 通过 ``read_skill`` / 召回后 call_skill 按需取）

    R2 cache 声明：inline / deferred 由 ``effective_child_recall`` 在 **pre-turn**
    据 entry 静态声明 + 可见 child 数裁定，整 turn 稳定——这是 system prompt 的**静态
    形状差异**（每 skill 属性），**不是 mid-turn cache 失效**，故本函数不返回
    ``CompressionResult``。同一 entry 跨 turn 走同一分支 → 缓存前缀稳定。

    **默认语义（设计稿 §3.1）**：``has_recall_backend=False``（默认）时召回后端是
    「工作记忆 / LLM 注意力」= inline——即便 child 很多也内联全列（LLM 自己找），
    **不**走 deferred、**不**提示 search_skills。只有业务注入 SkillRecall 后端
    （``has_recall_backend=True``）才可能 deferred（见 ``effective_child_recall``）。

    Args:
        recall_threshold: ``auto`` 模式下切到 deferred 的可见 child 数阈值
            （业务可配，turn 层用 pool 注入值覆盖默认）。
        has_recall_backend: 是否注入了 SkillRecall 召回后端。``False`` → 默认 inline
            （LLM 自己找）；显式 ``child_recall=deferred`` 但无后端会抛
            ``SkillValidationError``（见 ``effective_child_recall``）。

    spec Requirement: 装配顺序 = system_instructions → entry_skill →
    available_child_skills → dispatch_policy。``instructions=None`` 或空时
    SHALL 不出现 ``<system_instructions>`` 子串（向后兼容）。
    """
    from taifeng.skill.visibility import (
        effective_child_recall,
        visible_child_skills,
    )

    # 单一真相：inline / deferred 两条路径同源同过滤，得到可见 child 列表
    visible = visible_child_skills(entry, snapshot, capabilities)
    mode = effective_child_recall(
        entry,
        child_count=len(visible),
        threshold=recall_threshold,
        has_recall_backend=has_recall_backend,
    )
    if mode == "deferred":
        # deferred：不列 child，提示用 search_skills 召回（N=可见 child 数）
        child_block = _DEFERRED_CHILD_BLOCK.format(child_count=len(visible))
    else:
        # inline：逐字保持「- `id`: desc」格式（向后兼容，现有 prompt 测试不变）
        child_lines = [f"- `{v.skill_id}`: {v.description}" for v in visible]
        child_block = _INLINE_CHILD_BLOCK.format(
            child_lines="\n".join(child_lines) if child_lines else "  (none)"
        )
    body = SKILLS_INSTRUCTIONS_HEADER.format(
        id=entry.id,
        name=entry.name,
        body=entry.body,
        child_block=child_block,
        max_depth=entry.max_call_depth,
    )
    if instructions:
        # 注：resolver.resolve 已经按 priority 升序返回；这里不重排避免破坏调用方
        # 的稳定语义（spec: 稳定排序，priority 相等保持 layers 出现顺序）。
        return _render_instructions_block(instructions) + body
    return body


TOOL_IMAGE_OMITTED_TEMPLATE = (
    "<{count} image(s) omitted: this model cannot receive images in tool results>"
)
"""能力不足时的 in-band 占位符 —— **兜底网，不是主路径**。

主路径是 G4a 模态门控：声明 ``requires.modalities`` 的 skill 在拿不到图片的
环境下压根不被 offer，配置错误在路由期就暴露。本占位符只覆盖门控之外的残余
场景（skill 未声明要求、热重载换了 client、业务自建 RuntimeCapabilities 漏报）。

参照 codex ``sanitize_mcp_tool_result_for_model``（差异 Y：taifeng 按
``tool_output_modalities`` 判定，而非按 provider 硬编码）。**不抛异常**的理由：
模型不支持图片是选型事实而非错误，炸掉整个 turn 在多 skill 场景下会让一条
专科轨拖垮整个 join barrier；占位符让失败留在轨内，且模型能看见「这里本来有图」。
准入期失败（策略未启用 / 校验不过）仍然如实抛 —— 见 ``admit_tool_attachments``。
"""


def _extract_images(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从 item payload 取出 kind=="image" 的附件，非法形状按无附件处理。"""
    raw = payload.get("attachments", [])
    if not isinstance(raw, list):
        return []
    return [
        attachment
        for attachment in raw
        if isinstance(attachment, dict) and attachment.get("kind") == "image"
    ]


def _to_image_parts(
    images: list[dict[str, Any]], policy: ImageInputPolicy
) -> list[ImagePart]:
    """canonical attachment payload → provider-neutral ImagePart（含 admission）。"""
    attachments = [ImageAttachmentV1.model_validate(image) for image in images]
    return [
        ImagePart(
            media_type=image.attachment.media_type,
            base64_data=image.attachment.content,
            size=image.attachment.size,
            sha256=image.attachment.sha256,
            detail=image.attachment.detail,
        )
        for image in admit_image_attachments(attachments, policy)
    ]


def _tool_output_content(
    it: ResponseItem,
    *,
    image_input_policy: ImageInputPolicy,
    model_capabilities: ModelCapabilities,
) -> str | list[TextPart | ImagePart]:
    """``function_call_output`` 的内容投影（Chat / Responses 两条路径共用）。

    - 无附件 → 裸字符串（与既有逐位一致）
    - 有附件且能力足够 → 文本在首项、图片按序在后的 parts
    - 有附件但能力不足 → 文本 + in-band 占位符（见 ``TOOL_IMAGE_OMITTED_TEMPLATE``）
    """
    text = str(it.payload.get("output", ""))
    images = _extract_images(it.payload)
    if not images:
        return text
    if "image" not in model_capabilities.tool_output_modalities:
        notice = TOOL_IMAGE_OMITTED_TEMPLATE.format(count=len(images))
        return f"{text}\n{notice}" if text else notice
    parts: list[TextPart | ImagePart] = []
    if text:
        # 空文本不生成 TextPart —— 空项白占 API 数组槽位
        parts.append(TextPart(text=text))
    parts.extend(_to_image_parts(images, image_input_policy))
    return parts


def _item_to_api_message(
    it: ResponseItem,
    *,
    image_input_policy: ImageInputPolicy,
    model_capabilities: ModelCapabilities,
) -> ApiMessage | None:
    """非采样产出的单条 ResponseItem → ApiMessage;记账类 kind 返回 None。

    assistant_message / function_call / function_call_output 在
    ``history_to_api_messages`` 主循环内特殊处理(同轮合并),不走本函数。
    """
    if it.kind == "user_message":
        text = str(it.payload.get("text", ""))
        images = _extract_images(it.payload)
        if not images:
            return ApiMessage(role="user", content=text)
        # user 消息侧维持既有语义：能力不足**抛错**而非降级。与工具侧的差别是
        # 有意的——用户明确塞了图却看不到，属输入被吞，必须让调用方知道；工具
        # 侧的图是 agent 自己取的，降级留在轨内更合适。
        if "image" not in model_capabilities.input_modalities:
            from taifeng.llm.errors import UnsupportedModalityError

            raise UnsupportedModalityError("model client does not support image input")
        parts: list[TextPart | ImagePart] = []
        if text:
            parts.append(TextPart(text=text))
        parts.extend(_to_image_parts(images, image_input_policy))
        return ApiMessage(role="user", content=parts)
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
    tool_call = {
        "id": it.payload.get("call_id", ""),
        "type": "function",
        "function": {
            "name": it.payload.get("name", ""),
            "arguments": it.payload.get("arguments", "{}"),
        },
    }
    extra_content = it.payload.get("extra_content")
    if isinstance(extra_content, dict):
        tool_call["extra_content"] = extra_content
    return tool_call


def history_to_api_messages(
    items: Iterable[ResponseItem],
    *,
    include_reasoning: bool = True,
    image_input_policy: ImageInputPolicy | None = None,
    model_capabilities: ModelCapabilities | None = None,
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
    return _convert_history(
        items,
        include_reasoning=include_reasoning,
        image_input_policy=image_input_policy or DISABLED_IMAGE_POLICY,
        model_capabilities=model_capabilities or TEXT_ONLY_CAPABILITIES,
    )[0]


def _convert_history(
    items: Iterable[ResponseItem],
    *,
    include_reasoning: bool,
    image_input_policy: ImageInputPolicy = DISABLED_IMAGE_POLICY,
    model_capabilities: ModelCapabilities = TEXT_ONLY_CAPABILITIES,
) -> tuple[list[ApiMessage], list[int]]:
    """转换循环的单一来源:返回 ``(messages, source_indexes)``。

    ``source_indexes[i]`` = 产出 ``messages[i]`` 的 history 下标;合并消息取
    开窗 assistant_message(孤立 fc 时取该 fc 自身)的下标。cache anchor 据此
    把 history 坐标换算到 messages 坐标(cache-anchor-message-index)——独立
    的映射函数会与本循环的跳过/合并规则双实现漂移,故收敛在同一循环内。
    """
    out: list[ApiMessage] = []
    # 与 out 等长:每条产出消息的来源 history 下标
    src: list[int] = []
    # 暂存待附着的 reasoning 文本;开窗(assistant 消息产出)时附上并清空
    pending_reasoning: str | None = None
    # 当前采样轮的 assistant 消息在 out 中的下标(合并窗口);
    # user/system/compacted 产出即关窗,fco 不关窗(同轮并行 fc 的配对序
    # 是 fc,fco,fc,fco 交错),记账类 item(suspension 等)跨过保窗
    window_idx: int | None = None
    for idx, it in enumerate(items):
        if it.kind == "reasoning":
            if include_reasoning:
                pending_reasoning = str(it.payload.get("text", "")) or None
            continue
        if it.kind == "assistant_message":
            out.append(
                ApiMessage(
                    role="assistant",
                    content=str(it.payload.get("text", "")),
                    reasoning=pending_reasoning,
                )
            )
            src.append(idx)
            window_idx = len(out) - 1
            pending_reasoning = None
            continue
        if it.kind == "function_call":
            tc = _fc_to_tool_call(it)
            if window_idx is not None:
                # 并入合并窗口:不新增消息,来源下标保持窗口起点(am)
                tgt = out[window_idx]
                tgt.tool_calls = [*(tgt.tool_calls or []), tc]
            else:
                # 无前导 assistant 的孤立 fc(剪枝后的旧数据):独立成消息,
                # 自身即本轮的合并窗口(后续同轮 fc 仍归并到它)
                out.append(
                    ApiMessage(
                        role="assistant",
                        content="",
                        tool_calls=[tc],
                        reasoning=pending_reasoning,
                    )
                )
                src.append(idx)
                window_idx = len(out) - 1
                pending_reasoning = None
            continue
        if it.kind == "function_call_output":
            out.append(
                ApiMessage(
                    role="tool",
                    content=_tool_output_content(
                        it,
                        image_input_policy=image_input_policy,
                        model_capabilities=model_capabilities,
                    ),
                    tool_call_id=str(it.payload.get("call_id", "")),
                )
            )
            src.append(idx)
            continue
        msg = _item_to_api_message(
            it,
            image_input_policy=image_input_policy,
            model_capabilities=model_capabilities,
        )
        if msg is None:
            # 记账类 item 对 LLM 视图不存在,跨过它们保窗保 pending(确定性)
            continue
        # user/system/compacted 产出 = 新对话段:关窗 + 孤儿 reasoning 结算丢弃
        window_idx = None
        pending_reasoning = None
        out.append(msg)
        src.append(idx)
    return out, src


def _metadata_sample_id(item: ResponseItem, key: str) -> str | None:
    """读取 Responses 保留 sample metadata，并拒绝畸形值。"""
    value = item.metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidHistoryError(f"{key} must be a non-empty string")
    return value


def _provider_output_index(item: ResponseItem, fallback: int) -> int:
    """读取 provider output index；legacy 记录使用确定性 history 下标。"""
    value = item.metadata.get("provider_output_index", fallback)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidHistoryError("provider_output_index must be non-negative")
    return value


def _history_to_api_input_items(
    history: list[ResponseItem],
    *,
    image_input_policy: ImageInputPolicy,
    model_capabilities: ModelCapabilities,
) -> tuple[list[ApiInputItem], list[int]]:
    """把 durable history 投影成 Responses/状态门禁使用的严格有序 Items。"""
    output: list[ApiInputItem] = []
    sources: list[int] = []
    current_sample: str | None = None
    call_samples: dict[str, str] = {}
    for index, item in enumerate(history):
        if item.kind == "reasoning":
            current_sample = _metadata_sample_id(item, "llm_sample_id")
            raw_state = item.payload.get("provider_state")
            if raw_state is not None:
                if current_sample is None:
                    raise InvalidHistoryError("provider state requires llm_sample_id")
                output.append(
                    ApiProviderStateItem(
                        sample_id=current_sample,
                        output_index=_provider_output_index(item, index),
                        state=ProviderStateEnvelope.model_validate(raw_state),
                    )
                )
                sources.append(index)
            continue
        if item.kind == "assistant_message":
            current_sample = _metadata_sample_id(item, "llm_sample_id") or current_sample
            output.append(
                ApiMessageItem(
                    role="assistant",
                    content=str(item.payload.get("text", "")),
                    sample_id=current_sample,
                    output_index=_provider_output_index(item, index),
                )
            )
            sources.append(index)
            continue
        if item.kind == "function_call":
            call_id = str(item.payload.get("call_id", ""))
            sample_id = _metadata_sample_id(item, "llm_sample_id") or current_sample
            if not sample_id:
                sample_id = f"legacy:sample:{index}"
            if not call_id or call_id in call_samples:
                raise InvalidHistoryError("function call ids must be non-empty and unique")
            call_samples[call_id] = sample_id
            current_sample = sample_id
            output.append(
                ApiFunctionCallItem(
                    call_id=call_id,
                    name=str(item.payload.get("name", "")),
                    arguments=str(item.payload.get("arguments", "{}")),
                    sample_id=sample_id,
                    output_index=_provider_output_index(item, index),
                )
            )
            sources.append(index)
            continue
        if item.kind == "function_call_output":
            call_id = str(item.payload.get("call_id", ""))
            origin = _metadata_sample_id(item, "origin_llm_sample_id")
            origin = origin or call_samples.get(call_id)
            if not call_id or not origin:
                raise InvalidHistoryError("function output has no matching sample")
            output.append(
                ApiFunctionCallOutputItem(
                    call_id=call_id,
                    output=_tool_output_content(
                        item,
                        image_input_policy=image_input_policy,
                        model_capabilities=model_capabilities,
                    ),
                    origin_sample_id=origin,
                )
            )
            sources.append(index)
            continue
        message = _item_to_api_message(
            item,
            image_input_policy=image_input_policy,
            model_capabilities=model_capabilities,
        )
        if message is not None and message.role != "tool":
            output.append(ApiMessageItem(role=message.role, content=message.content))
            sources.append(index)
            current_sample = None
    return output, sources


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
    recall_threshold: int = DEFAULT_RECALL_THRESHOLD,
    has_recall_backend: bool = False,
    image_input_policy: ImageInputPolicy | None = None,
    model_input_capabilities: ModelCapabilities | None = None,
) -> ApiRequest:
    resolved_policy = image_input_policy or DISABLED_IMAGE_POLICY
    resolved_capabilities = model_input_capabilities or TEXT_ONLY_CAPABILITIES
    # G4a 模态门控：把 client 自己声明的能力派生成标签并入 RuntimeCapabilities，
    # 使「要图片工具输出的 skill」在拿不到图片的环境下根本不出现在可派发列表里
    # （路由期 fail-fast，优于派发后在渲染期降级）。业务无需手工同步这些标签——
    # 同一事实若有「client 声明」与「业务汇报」两个来源必然漂移；业务自定义
    # 标签保留，两者取并集。
    resolved_runtime = capabilities
    if capabilities is not None:
        # 局部 import：与本文件 render_system_prompt 内的 skill.visibility 同形，
        # 避免 loop → skill 的模块级导入环。
        from taifeng.skill.eligibility import derive_modality_tags

        resolved_runtime = replace(
            capabilities,
            modalities=(
                capabilities.modalities | derive_modality_tags(resolved_capabilities)
            ),
        )
    system_prompt = render_system_prompt(
        entry,
        snapshot,
        instructions=instructions,
        capabilities=resolved_runtime,
        recall_threshold=recall_threshold,
        has_recall_backend=has_recall_backend,
    )
    contains_provider_state = any(
        item.kind == "reasoning" and item.payload.get("provider_state") is not None
        for item in history
    )
    if contains_provider_state and not resolved_capabilities.accepts_provider_state:
        raise InvalidHistoryError(
            "model client does not accept persisted provider state"
        )
    use_ordered_items = (
        resolved_capabilities.protocol == "responses" or contains_provider_state
    )
    input_items: list[ApiInputItem] | None = None
    if use_ordered_items:
        input_items, source_indexes = _history_to_api_input_items(
            history,
            image_input_policy=resolved_policy,
            model_capabilities=resolved_capabilities,
        )
        messages = []
    else:
        messages, source_indexes = _convert_history(
            history,
            include_reasoning=reasoning_passback,
            image_input_policy=resolved_policy,
            model_capabilities=resolved_capabilities,
        )

    # cache anchor 坐标换算(cache-anchor-message-index):anchor 是 history
    # 下标(压缩 anchor_preserved_until,[0, N) 为稳定前缀),CacheBreakpoint.index
    # 是 messages 下标(anthropic 据此打 cache_control)。打点位置 = 稳定前缀
    # 产出的最后一条消息;前缀无产出消息(N<=0 / 全是记账 item)则不打点。
    breakpoints: list[CacheBreakpoint] = []
    if cache_anchor_index > 0:
        for i in range(len(source_indexes) - 1, -1, -1):
            if source_indexes[i] < cache_anchor_index:
                breakpoints.append(CacheBreakpoint(index=i))
                break

    # K3 prefetch（page-in）：把取回的长期记忆作为**尾部** system 消息注入，
    # 不动 system_prompt 头部（R2 cache-aware：变动的 prefetch 不破坏 cached 前缀）。
    if prefetched_memory:
        memory_content = f"<retrieved_memory>\n{prefetched_memory}\n</retrieved_memory>"
        if input_items is not None:
            input_items.append(ApiMessageItem(role="system", content=memory_content))
        else:
            messages.append(ApiMessage(role="system", content=memory_content))

    common = {
        "model": model,
        "system_prompt": [system_prompt],
        "tools": tools,
        "parallel_tool_calls": True,
        "cache_breakpoints": breakpoints,
    }
    if input_items is not None:
        return ApiRequest(input_items=input_items, **common)
    return ApiRequest(messages=messages, **common)
