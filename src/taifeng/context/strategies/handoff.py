"""HandoffCompactionStrategy —— codex 范式的 LLM-to-LLM 接力压缩。

实现要点：
    1. mid-turn 模式：从 cache_anchor_index 开始压缩 tail，不动 head
    2. pre-turn / manual：可以动 head
    3. tool_use / tool_result 配对边界保护（claw-code 实测发现）
    4. 调用 LLM 生成结构化 4 段摘要（进度 / 决策 / 待办 / 引用）

参照：
    - codex templates/compact/prompt.md
    - claw-code crates/runtime/src/compact.rs::_walk_back_to_safe_boundary
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from taifeng.context.budget import estimate_history_tokens
from taifeng.context.compressor import (
    CompressionContext,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.truncate import truncate_middle
from taifeng.conversation.models import ResponseItem, compacted
from taifeng.llm.client import ModelClient
from taifeng.llm.types import ApiMessage, ApiRequest
from taifeng.loop.cancellation import CancellationToken

logger = logging.getLogger(__name__)


HANDOFF_SYSTEM_PROMPT_ZH = """你正在接手一段被压缩的对话历史。请生成一份结构化摘要，作为接力提示词。

## 必须保留
1. 所有进行中任务的当前状态（进行中 / 已完成 / 已失败）
2. 批量操作的进度（已处理 N/M 项）
3. 最近的用户请求和未回应的问题
4. 所有已做出的决策及其理由
5. 已确认的约束条件和业务规则
6. 所有不透明标识符：UUID、hash、文件路径、URL、分支名、ID —— 原样保留，不得缩写
7. 工具调用的关键结果（成功 / 失败 + 要点，不含中间过程）

## 必须去除
1. 中间推理过程和思考链
2. 重复的信息和冗余描述
3. 已完成步骤的详细过程
4. 工具调用的原始大段输出

## 输出格式（严格按此结构）

## 进度 (Progress)
- [当前任务及其状态]

## 决策 (Decisions)
- [已做出的决策及理由]

## 待办 (TODO)
- [未完成的任务、用户提出但未回应的请求]

## 引用 (References)
- [所有不可缩写的标识符列表 —— UUID / 文件路径 / hash / ID 等]

## 工具结果摘要 (Tool Results)
- [关键工具调用的精简结果]

## 语言
使用与原始对话相同的主要语言。

## 末尾
在摘要末尾添加一行：「此摘要替换了原始的 N 条消息（位置 [start, end)）。」
"""


def _walk_back_to_safe_boundary(history: list[ResponseItem], cut: int) -> int:
    """回退切分点直到不切断 function_call / function_call_output 配对,
    以及 reasoning / assistant_message 配对。

    参照：claw-code crates/runtime/src/compact.rs(fc/fco 部分;reasoning
    配对保护是 reasoning-content-passback 的 taifeng 扩展)
    """
    while cut > 0 and cut < len(history):
        item = history[cut]
        if item.kind != "function_call_output":
            # reasoning↔assistant 配对保护:切分点指向 assistant_message 且其
            # 前一项是配对 reasoning 时一并保留——thinking 模型要求带 tool_calls
            # 的 assistant 消息续传时回传 reasoning_content,剪掉 reasoning 会使
            # 压缩后历史的续传可能被 provider 拒(reasoning-content-passback)
            if (
                item.kind == "assistant_message"
                and history[cut - 1].kind == "reasoning"
            ):
                cut -= 1
                continue
            return cut
        # 检查它的 function_call 是否在保留段中
        call_id = item.payload.get("call_id")
        if call_id is None:
            return cut
        # 如果配对的 function_call 在 [0:cut) 中（即被切走），则回退
        paired = any(
            h.kind == "function_call" and h.payload.get("call_id") == call_id
            for h in history[:cut]
        )
        if paired:
            # function_call 在前面（被切），保留 output 会孤立 → 把 output 也保留
            cut -= 1
            continue
        return cut
    return cut


def _format_messages_for_summary(items: list[ResponseItem]) -> str:
    lines: list[str] = []
    for i, it in enumerate(items):
        role_label = {
            "user_message": "用户",
            "assistant_message": "助手",
            "function_call": "工具调用",
            "function_call_output": "工具结果",
            "reasoning": "推理",
            "system_injection": "系统注入",
            "compacted": "（已压缩摘要）",
        }.get(it.kind, it.kind)
        header = f"--- [{i + 1}] {role_label} ---"
        lines.append(header)
        if it.kind == "user_message" or it.kind == "assistant_message":
            lines.append(str(it.payload.get("text", "")))
        elif it.kind == "function_call":
            lines.append(
                f"调用 {it.payload.get('name')}(call_id={it.payload.get('call_id')}): "
                f"{it.payload.get('arguments', '')}"
            )
        elif it.kind == "function_call_output":
            output = str(it.payload.get("output", ""))
            # G6b：中段截断，保尾部（错误/结论常在尾），优于朴素 [:1000]
            output = truncate_middle(output, 1500)
            lines.append(f"call_id={it.payload.get('call_id')}: {output}")
        else:
            lines.append(str(it.payload))
        lines.append("")
    return "\n".join(lines)


# === 摘要质量审计（G1a；参照 openclaw compaction-safeguard-quality.ts）===

# 必备分段标题 —— 缺失视为「非致命」质量警告（仅记录，不强制重生成）。
_REQUIRED_SECTIONS: tuple[str, ...] = ("进度", "决策", "待办", "引用")

# 不透明标识符模式 —— 这些一旦在摘要中丢失即不可恢复，视为「致命」缺陷。
# 仅收高信号项（URL / UUID / 长 hex hash），避免把普通词误判为标识符触发误重生成。
_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://[^\s)\]}'\"，。、]+"),  # URL（剔除中文标点收尾）
    re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    ),  # UUID
    re.compile(r"\b[0-9a-f]{12,}\b"),  # 长 hex（sha / commit / hash）
)


def _extract_opaque_identifiers(text: str) -> set[str]:
    """抽取文本中不可恢复的不透明标识符（URL / UUID / 长 hex）。"""
    found: set[str] = set()
    for pat in _IDENTIFIER_PATTERNS:
        found.update(pat.findall(text))
    return found


@dataclass(frozen=True)
class SummaryAudit:
    """摘要质量审计结果。

    ok 仅由「标识符是否完整保留」决定 —— 缺分段是 style 问题（非致命），
    丢标识符是 data-loss（致命，需重生成 / 最终拒绝压缩）。
    """

    ok: bool
    missing_sections: tuple[str, ...]
    missing_identifiers: tuple[str, ...]


def _audit_summary_quality(
    summary: str, source_items: list[ResponseItem]
) -> SummaryAudit:
    """审计 LLM 生成的接力摘要质量。

    Args:
        summary: LLM 产出的摘要全文
        source_items: 被压缩的原始消息（用于抽取必须保留的标识符）

    Returns:
        SummaryAudit —— missing_identifiers 非空即 ``ok=False``。
    """
    missing_sections = tuple(s for s in _REQUIRED_SECTIONS if s not in summary)
    source_text = _format_messages_for_summary(source_items)
    src_ids = _extract_opaque_identifiers(source_text)
    summary_ids = _extract_opaque_identifiers(summary)
    missing_identifiers = tuple(
        sorted(i for i in src_ids if i not in summary_ids)
    )
    return SummaryAudit(
        ok=not missing_identifiers,
        missing_sections=missing_sections,
        missing_identifiers=missing_identifiers,
    )


def _regeneration_feedback(audit: SummaryAudit) -> str:
    """根据上一版审计结果，构造给下一轮 LLM 的纠正反馈。"""
    parts: list[str] = ["上一版摘要存在以下问题，请修正后重新输出完整摘要："]
    if audit.missing_identifiers:
        ids = "、".join(audit.missing_identifiers)
        parts.append(
            f"- 遗漏了必须原样保留的不透明标识符：{ids}。"
            "请在「## 引用」分段中逐一补全，不得缩写。"
        )
    if audit.missing_sections:
        secs = "、".join(audit.missing_sections)
        parts.append(f"- 缺失必备分段：{secs}。请补齐这些分段标题。")
    return "\n".join(parts)


class HandoffCompactionStrategy:
    """LLM 接力压缩策略。"""

    name = "handoff"
    priority = 100

    def __init__(
        self,
        model_client: ModelClient,
        *,
        model: str | None = None,
        thread_id_provider: object | None = None,
        quality_max_attempts: int = 2,
    ) -> None:
        """
        Args:
            model_client: 用于生成接力摘要的 LLM client
            model: 摘要模型；None → 用 client 默认
            quality_max_attempts: 摘要质量审计的最大生成次数（含首轮）。
                ≥2 时，首轮若丢失不透明标识符会带反馈重生成；耗尽仍丢失
                则拒绝压缩（保留原历史）。设为 1 即关闭重生成。
        """
        self._client = model_client
        self._model = model
        self._quality_max_attempts = max(1, quality_max_attempts)

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        if ctx.budget.is_soft_exceeded(ctx.token_estimate):
            return CompressionTrigger(
                reason="token_limit",
                threshold_pct=ctx.token_estimate / max(ctx.budget.context_window, 1),
            )
        if ctx.phase == "manual":
            return CompressionTrigger(reason="user_request", threshold_pct=0.0)
        return None

    def _fail(self, ctx: CompressionContext, reason: str) -> CompressionResult:
        """压缩失败的统一构造 —— 不改 history，调用方据此保留原历史。"""
        return CompressionResult(
            success=False,
            cache_invalidated=False,
            anchor_preserved_until=ctx.cache_anchor_index,
            reason=reason,
        )

    def _compute_boundaries(
        self, ctx: CompressionContext, injection: InitialContextInjection
    ) -> tuple[int, int] | CompressionResult:
        """计算可压缩区间 [start, end)；不可压缩时返回失败 CompressionResult。"""
        history = ctx.history
        if len(history) <= ctx.budget.preserve_tail_messages + 1:
            return self._fail(ctx, "too_few_to_compact")

        if injection == InitialContextInjection.DO_NOT_INJECT:
            # mid-turn：不能动 cache_anchor 之前的内容。
            # anchor=-1（从未压缩/无锚）必须钳到 0：负值会让区间切片走负索引，
            # 压缩产物不缩反增（首次 overflow 自愈必死）；无锚即无可保，从头压合法
            start = max(ctx.cache_anchor_index, 0)
        else:
            # pre-turn / manual：从 head 开始（但避开 system_injection 段）
            start = 0
            while start < len(history) and history[start].kind == "system_injection":
                start += 1

        end = len(history) - ctx.budget.preserve_tail_messages
        # 配对边界保护：切点不切断 function_call / function_call_output
        end = _walk_back_to_safe_boundary(history, end)
        if end - start < 2:
            return self._fail(ctx, "boundary_too_narrow")
        return start, end

    async def _generate_summary(
        self,
        to_compress: list[ResponseItem],
        start: int,
        end: int,
        *,
        feedback: str = "",
    ) -> tuple[str, str | None]:
        """调一次 LLM 生成摘要，返回 (summary_text, error_reason)。

        error_reason 非 None 表示生成失败（LLM error / 异常）。
        """
        formatted = _format_messages_for_summary(to_compress)
        content = (
            f"以下是需要压缩的对话历史（共 {len(to_compress)} 条，"
            f"覆盖原始位置 [{start}, {end})）：\n\n{formatted}"
        )
        if feedback:
            content = f"{feedback}\n\n{content}"
        cancel = CancellationToken(name="compact")
        request = ApiRequest(
            # 对齐采样路径（turn.py 用 entry_skill.model or ""）：空字符串让 provider
            # 用其构造时配置的默认 model。此前的 "auto" 占位在不认 "auto" 的网关
            # （如 new-api distributor）会 model_not_found，导致真实压缩失败（含 A1
            # force_compress 走的同一路径）。mock 显式传 model，不受影响。
            model=self._model or "",
            system_prompt=[HANDOFF_SYSTEM_PROMPT_ZH],
            messages=[ApiMessage(role="user", content=content)],
            tools=[],
            parallel_tool_calls=False,
        )
        summary_text = ""
        try:
            async with self._client.session(cancel=cancel, model=self._model) as s:
                async for ev in s.stream(request):
                    if ev.kind == "text_delta":
                        summary_text += ev.data.get("text", "")
                    elif ev.kind == "error":
                        return "", f"llm_error: {ev.data.get('message')}"
        except Exception as e:  # noqa: BLE001 —— 压缩失败必须降级为保留历史
            logger.exception("handoff compaction LLM call failed")
            return "", f"exception: {e}"
        return summary_text.strip(), None

    def _build_result(
        self,
        ctx: CompressionContext,
        start: int,
        end: int,
        summary_text: str,
        injection: InitialContextInjection,
        warnings: tuple[str, ...],
    ) -> CompressionResult:
        """用通过质量审计的摘要构造成功的 CompressionResult。"""
        history = ctx.history
        if injection == InitialContextInjection.DO_NOT_INJECT:
            anchor_preserved = start - 1
            cache_invalidated = False
        else:
            anchor_preserved = -1  # head 被改写 → 全部失效
            cache_invalidated = True

        thread_id = history[0].thread_id if history else "unknown"
        summary_item = compacted(
            summary_text,
            thread_id=thread_id,
            replaced_range=(start, end),
            cache_invalidated=cache_invalidated,
        )
        new_history = history[:start] + [summary_item] + history[end:]
        logger.info(
            "handoff compaction: %d → %d tokens, removed=%d messages, warnings=%s",
            ctx.token_estimate,
            estimate_history_tokens(new_history),
            end - start,
            warnings,
        )
        return CompressionResult(
            success=True,
            cache_invalidated=cache_invalidated,
            anchor_preserved_until=anchor_preserved,
            new_history=new_history,
            removed_item_count=end - start,
            summary_item_id=summary_item.id,
            quality_warnings=warnings,
        )

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult:
        bounds = self._compute_boundaries(ctx, injection)
        if isinstance(bounds, CompressionResult):
            return bounds
        start, end = bounds
        to_compress = ctx.history[start:end]

        # 质量审计 + 有界重生成：丢失不透明标识符（致命）才触发重生成 / 最终拒绝。
        audit: SummaryAudit | None = None
        summary_text = ""
        for attempt in range(self._quality_max_attempts):
            feedback = _regeneration_feedback(audit) if audit and attempt > 0 else ""
            summary_text, err = await self._generate_summary(
                to_compress, start, end, feedback=feedback
            )
            if err is not None:
                return self._fail(ctx, err)
            if not summary_text:
                return self._fail(ctx, "empty_summary")
            audit = _audit_summary_quality(summary_text, to_compress)
            if audit.ok:
                break

        # 耗尽重试仍丢标识符 → 拒绝压缩（保留历史优于静默丢数据）
        if audit is not None and audit.missing_identifiers:
            return self._fail(
                ctx,
                "quality_audit_failed_identifiers: "
                + "、".join(audit.missing_identifiers),
            )

        warnings = audit.missing_sections if audit else ()
        return self._build_result(
            ctx, start, end, summary_text, injection, warnings
        )
