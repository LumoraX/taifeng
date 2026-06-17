"""search_skills 工具 —— 相位 2 skill 发现链的入口（deferred 召回）。

定位：当 caller composite skill 的 ``child_skills`` 多到「装不进一次 prompt 的
inline 列表」时，改由 LLM 主动调 ``search_skills(query)`` 按需召回 top_k 个最相关的
子 skill 候选，再据返回决定 ``call_skill`` 派发哪一个。

与 inline 列表**同源同过滤**（C2 红线，避免 deferred 成为 G4 旁路）：召回池由
``skill.visibility.visible_child_skills`` 构建——它是 ``loop/prompt.py`` inline 列表
那套 G4 过滤的唯一实现，故 deferred 召回拿不到本应被隐藏（``model_invocable=False``）
或不满足 ``requires`` 的子 skill。

ToolContext.extras 必须含（由 TurnRunner._build_tool_context 注入）：
    - ``skill_snapshot``: SkillSnapshot —— 据 child_skills id 解析定义。
    - ``current_skill``: SkillDefinition —— 当前 caller entry，提供 child_skills 白名单。
可选：
    - ``capabilities``: RuntimeCapabilities | None —— 提供时施加 G4a requires 过滤。
    - ``dispatcher``: 持 ``_emit`` 的 TurnRunner —— 打两个可观测事件；缺失则静默 noop。

R1 业务零侵入：纯通用 skill 发现原语，不含任何业务概念。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from taifeng.skill.recall import RecallEntry
from taifeng.skill.visibility import visible_child_skills
from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from taifeng.skill.definition import SkillDefinition
    from taifeng.skill.eligibility import RuntimeCapabilities
    from taifeng.skill.recall import SkillRecall
    from taifeng.skill.registry import SkillSnapshot


async def _emit_event(ctx: ToolContext, kind: str, data: dict[str, Any]) -> None:
    """通过 dispatcher (TurnRunner) emit 一条 EventMsg；缺失时 noop。

    与 ``call_skill._emit_event`` 同范式：只构造 msg 实例传给 ``dispatcher._emit``，
    由 TurnRunner._emit 负责包 ``EventMsg(submission_id, msg=msg)``——**禁止**预包
    EventMsg，否则双重 wrap 触发 pydantic discriminator 错。

    Args:
        ctx: 工具上下文（从 extras 取 dispatcher）。
        kind: 事件 kind（``skill_search_invoked`` / ``skill_candidates_returned``）。
        data: 事件 data 负载。
    """
    dispatcher = ctx.extras.get("dispatcher")
    if dispatcher is None:
        # 无 dispatcher（如单测直接调 handler）：可观测打点降级为 noop，不影响召回
        return
    # 延迟 import 防止 tool → loop 的 import 期循环依赖
    from taifeng.loop.event import SkillCandidatesReturned, SkillSearchInvoked

    msg_cls_map: dict[str, type] = {
        "skill_search_invoked": SkillSearchInvoked,
        "skill_candidates_returned": SkillCandidatesReturned,
    }
    cls = msg_cls_map.get(kind)
    if cls is None:
        return
    msg = cls(data=data)
    emit = getattr(dispatcher, "_emit", None)
    if emit is None:
        return
    try:
        await emit(msg)
    except Exception:
        # emit 失败不影响召回主流程（可观测降级，禁止吞掉召回结果）
        import logging

        logging.getLogger(__name__).exception("emit %s failed", kind)


def _clamp_top_k(raw: Any, *, default_top_k: int, max_top_k: int) -> int:
    """解析并钳制 LLM 传入的 top_k：非法 / 缺省 → default；超上界 → 钳到 max。

    Args:
        raw: LLM 传入的原始 top_k（可能缺省 / 非整数 / 越界）。
        default_top_k: 缺省或非法时的默认值。
        max_top_k: 钳制上界（防 LLM 一次召回过多撑爆 context）。

    Returns:
        合法的 top_k：``1 <= 返回值 <= max_top_k``。
    """
    # 缺省或类型非法（含 bool，bool 是 int 子类，单独排除）→ 取默认
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        value = default_top_k
    else:
        value = raw
    # 下界保护：非正数无意义，回落默认（默认本身保证为正由构造方负责）
    if value < 1:
        value = default_top_k
    # 上界钳制：超 max_top_k 一律压到 max_top_k
    return min(value, max_top_k)


def _make_search_skills_handler(
    recall: SkillRecall, *, default_top_k: int, max_top_k: int
) -> Any:
    """闭包出 search_skills handler，捕获注入的召回后端与 top_k 边界。"""

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """search_skills handler —— 构召回池 → 召回 → 透候选给 LLM。"""
        # ---- 参数校验 ----
        query = args.get("query")
        if not query or not isinstance(query, str):
            return ToolResult.error(
                "missing_or_invalid_argument: query", reason="bad_args"
            )
        top_k = _clamp_top_k(
            args.get("top_k"), default_top_k=default_top_k, max_top_k=max_top_k
        )

        # ---- 从 ctx.extras 取必需依赖 ----
        snapshot: SkillSnapshot | None = ctx.extras.get("skill_snapshot")
        if snapshot is None:
            return ToolResult.error(
                "skill_snapshot not in tool context", reason="config_error"
            )
        caller: SkillDefinition | None = ctx.extras.get("current_skill")
        if caller is None:
            return ToolResult.error(
                "current_skill not in tool context", reason="config_error"
            )
        # G4a 能力快照（可选）：提供时施加 requires 过滤，与 inline 列表一致
        capabilities: RuntimeCapabilities | None = ctx.extras.get("capabilities")

        # ---- 构召回池：caller.child_skills + G4 过滤（与 inline 列表同源同过滤）----
        # 召回池**仅含 caller 的 child_skills**（更窄），不是 reachable 全集——
        # 召回是「为 LLM 选下一个 call_skill 目标」服务，目标必须在白名单内。
        visible = visible_child_skills(caller, snapshot, capabilities)
        pool = [
            RecallEntry(skill_id=v.skill_id, description=v.description)
            for v in visible
        ]

        # ---- 可观测：发起检索打点（pool_size = 过滤后的可见池规模）----
        await _emit_event(
            ctx,
            "skill_search_invoked",
            {"query": query, "top_k": top_k, "pool_size": len(pool)},
        )

        # ---- 调召回后端（白名单封闭由内核钉死：pool 即可召回的全集）----
        candidates = await recall.recall(query, pool, top_k=top_k, cancel=ctx.cancel)

        # ---- 可观测：返回候选打点 ----
        await _emit_event(
            ctx,
            "skill_candidates_returned",
            {"count": len(candidates), "top_ids": [c.skill_id for c in candidates]},
        )

        # ---- 透候选给 LLM：不外露 score（仅审计/内部），保留 confidence ----
        payload = [
            {
                "skill_id": c.skill_id,
                "description": c.description,
                "confidence": c.confidence,
                "matched_snippet": c.matched_snippet,
            }
            for c in candidates
        ]
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            candidate_count=len(candidates),
        )

    return _handler


SEARCH_SKILLS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "检索词：用「能力关键词 + 同义词」（多词覆盖近义说法）以提高召回，"
                "例 'SQL注入 参数化 安全审查 注入风险'。**避免照抄用户口语原句**——"
                "口语与技能描述措辞常不重叠会召不中。没命中可换一组关键词再调一次。"
            ),
        },
        "top_k": {
            "type": "integer",
            "description": (
                "返回候选数上界（选填）。缺省用内核默认；超过内核上限会被自动钳制。"
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def make_search_skills_tool(
    recall: SkillRecall, *, default_top_k: int, max_top_k: int
) -> ToolSpec:
    """构造 search_skills 工具规范（相位 2 deferred 召回入口）。

    Args:
        recall: 可插拔召回后端（默认 KeywordSkillRecall，业务可替换为向量 / 外部检索）。
        default_top_k: LLM 未指定 top_k 时的默认返回候选数。
        max_top_k: top_k 钳制上界（防一次召回过多撑爆 context）。

    Returns:
        ``search_skills`` ToolSpec：``parallel_safe=True``（只读 snapshot + 召回）。
    """
    return ToolSpec(
        name="search_skills",
        description=(
            "据 query 召回当前 skill 的子 skill 候选（当可选子 skill 太多、未全部"
            "列在 available_child_skills 时用它发现目标）。返回候选含 skill_id / "
            "description / confidence / matched_snippet；据此再 call_skill 派发。"
            "可用不同关键词多次调用以精炼召回。"
        ),
        input_schema=SEARCH_SKILLS_SCHEMA,
        handler=_make_search_skills_handler(
            recall, default_top_k=default_top_k, max_top_k=max_top_k
        ),
        parallel_safe=True,  # 只读 snapshot + 召回，安全并行
        timeout_seconds=10.0,
    )
