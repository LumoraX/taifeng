"""SurgicalTrim 策略 —— 介于滑窗丢弃与 LLM 摘要之间的就地有损剪枝（手术刀档）。

参照（只学范式，不抄代码）：
    - openclaw pi-hooks/context-pruning/pruner.ts —— soft/hard 分级 + cache-TTL 对齐触发
    - hermes agent/context_compressor.py —— 压缩前 LLM-free tool-result md5 去重

与上游的差异：
    - **只改写 ``function_call_output`` 的 payload、永不删条目** —— fc/output 配对与
      条目顺序天然不变，不触发配对完整性回滚（G1b），resume 重放结构稳定（R5）。
    - 三 pass 收敛在单策略内（dedup → soft-trim → hard-clear），由 ratio 分级启用；
      orchestrator 仍按「策略 × priority」调度，不感知内部分级。
    - cache-TTL 触发自管 ``_last_trim_at``（注入时钟），不依赖 PromptCacheStats
      （后者记录 provider cache 读量，无「上次触碰时刻」语义）。

全程 LLM-free（这正是「便宜」的来源）；R4 取消采用协作式 asyncio 检查点
（pass 边界 ``await sleep(0)``，外部 task 取消在边界生效——CompressionContext
不携带 CancellationToken，且 context/ 不反向依赖 loop/）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from taifeng.context.compressor import (
    CompressionContext,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.injection import InitialContextInjection
from taifeng.context.placeholders import (
    DEDUP_PREFIX as _DEDUP_PREFIX,
)
from taifeng.context.placeholders import (
    PRUNED_PREFIX as _PRUNED_PREFIX,
)
from taifeng.context.placeholders import (
    is_placeholder as _is_placeholder,
)
from taifeng.context.truncate import truncate_middle

if TYPE_CHECKING:
    from collections.abc import Callable

    from taifeng.conversation.models import ResponseItem


def _build_name_index(history: list[ResponseItem]) -> dict[str, str]:
    """call_id → tool name 索引（function_call_output 自身不带 name，需回溯配对）。"""
    return {
        it.payload["call_id"]: it.payload["name"]
        for it in history
        if it.kind == "function_call"
    }


def _output_digest(item: ResponseItem) -> str:
    """fco 去重摘要 —— 必须同时覆盖文本与图片附件。

    只哈希 ``output`` 文本会把「文本相同、图片不同」的两条判成重复，静默丢掉
    一张不同的图（正确性 bug，不是优化问题）。附件以其 canonical ``sha256``
    参与：它本就是内容身份，无需二次哈希图片正文。排序后参与，使同一组图片的
    顺序差异不构成内容差异。

    非加密用途 —— 仅做内容指纹去重（对标 hermes md5[:12]）。
    """
    text = str(item.payload.get("output", ""))
    digests = sorted(
        str(attachment.get("sha256", ""))
        for attachment in item.payload.get("attachments") or []
    )
    material = "\0".join([text, *digests])
    return hashlib.md5(
        material.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]


def _rewrite(item: ResponseItem, new_output: str) -> ResponseItem:
    """就地替换 output payload —— 保留 item 其余字段（id / thread_id 等，R5 身份不变）。

    带附件时**一并丢弃附件**并在占位符里留计数痕迹：只剪文本会留下真正昂贵的
    那半边（一张图 ≈ 千级 token，文本仅数百字符），使本策略对含图会话形同虚设。
    丢弃是有损的——这正是 surgical_trim 的定位；需要无损请用 offload（但它不
    处理图片，见该策略说明）。
    """
    payload = dict(item.payload)
    attachments = payload.pop("attachments", None)
    if attachments:
        new_output = f"{new_output}[已剪枝 {len(attachments)} 张图片]"
    payload["output"] = new_output
    return item.model_copy(update={"payload": payload})


class SurgicalTrimStrategy:
    """就地有损剪枝策略：dedup → soft-trim → hard-clear 三 pass 分级。

    Args:
        priority: orchestrator 排序优先级。推荐配为最高（最便宜先试，剪后估算
            仍超时下一轮自然落到 handoff）。
        soft_trim_ratio: token 估算占 context_window 比例 ≥ 此值时启用 soft-trim
            （亦是 should_trigger 的触发阈值）。
        hard_clear_ratio: ratio ≥ 此值时 soft 升级为 hard-clear（整体占位符替换）。
        min_dedup_chars: 参与 md5 去重的 output 最小长度（去重 pass 恒启用——零成本）。
        head_chars / tail_chars: soft-trim 保留的头 / 尾字数。
        protect_tail_messages: 尾部保护条数 —— 最后 N 条内的 output 永不剪。
        allow_globs / deny_globs: 工具名 glob 白/黑名单（fnmatch 语义），deny 优先；
            「哪些工具可剪」是业务语义，由业务注入（R1）。
        allow_head_clear: 仅 pre_turn（BEFORE_LAST_USER_MESSAGE）下允许 hard-clear
            越过 cache anchor；越过时如实标 ``cache_invalidated=True``（R2）。
        cache_ttl_seconds: cache-TTL 对齐触发（opt-in）：距上次成功剪枝不足 ttl
            时 should_trigger 返回 None —— 把有损动作对齐到 cache 反正要过期的时刻。
        clock: 时间源（默认 ``time.monotonic``；测试注入假钟）。
    """

    name = "surgical_trim"

    def __init__(
        self,
        *,
        priority: int = 20,
        soft_trim_ratio: float = 0.3,
        hard_clear_ratio: float = 0.5,
        min_dedup_chars: int = 256,
        head_chars: int = 400,
        tail_chars: int = 200,
        protect_tail_messages: int = 4,
        allow_globs: tuple[str, ...] = ("*",),
        deny_globs: tuple[str, ...] = (),
        allow_head_clear: bool = False,
        cache_ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.priority = priority
        self._soft_ratio = soft_trim_ratio
        self._hard_ratio = hard_clear_ratio
        self._min_dedup_chars = min_dedup_chars
        self._head_chars = head_chars
        self._tail_chars = tail_chars
        self._protect_tail = protect_tail_messages
        self._allow_globs = allow_globs
        self._deny_globs = deny_globs
        self._allow_head_clear = allow_head_clear
        self._cache_ttl = cache_ttl_seconds
        self._clock = clock
        # 上次成功剪枝时刻（cache-TTL 闸用）；None = 从未剪过
        self._last_trim_at: float | None = None

    # ---- 触发 ----

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        """ratio ≥ soft_trim_ratio 即候选触发；启用 ttl 时再过 cache-TTL 闸。"""
        ratio = ctx.token_estimate / max(ctx.budget.context_window, 1)
        if ratio < self._soft_ratio:
            return None
        # cache-TTL 闸：距上次剪枝不足 ttl → cache 还活着，不白白破坏（R2）
        if (
            self._cache_ttl is not None
            and self._last_trim_at is not None
            and (self._clock() - self._last_trim_at) < self._cache_ttl
        ):
            return None
        return CompressionTrigger(reason="token_limit", threshold_pct=ratio)

    # ---- 可剪判定 ----

    def _tool_allowed(self, name: str) -> bool:
        """glob 判定：deny 优先于 allow。"""
        if any(fnmatchcase(name, g) for g in self._deny_globs):
            return False
        return any(fnmatchcase(name, g) for g in self._allow_globs)

    def _candidates(
        self,
        history: list[ResponseItem],
        names: dict[str, str],
        start: int,
        end: int,
    ) -> list[int]:
        """窗口 [start, end) 内可剪的 function_call_output 索引。

        排除：孤儿 output（call_id 回溯不到配对 fc → 不猜测、不剪）、
        glob 不允许的工具、已是占位符的条目（幂等）。
        """
        picked: list[int] = []
        for i in range(max(start, 0), min(end, len(history))):
            it = history[i]
            if it.kind != "function_call_output":
                continue
            name = names.get(it.payload["call_id"])
            if name is None or not self._tool_allowed(name):
                continue
            if _is_placeholder(it.payload["output"]):
                continue
            picked.append(i)
        return picked

    # ---- 三 pass ----

    def _dedup_pass(
        self, history: list[ResponseItem], candidates: list[int]
    ) -> int:
        """md5 去重：反向扫描保最新一份完整，更旧的换 duplicate 占位符。"""
        seen: dict[str, int] = {}
        deduped = 0
        for i in reversed(candidates):
            text = history[i].payload["output"]
            if len(text) < self._min_dedup_chars:
                continue
            digest = _output_digest(history[i])
            kept = seen.get(digest)
            if kept is None:
                seen[digest] = i  # 反扫首见 = 最新一份，保留
                continue
            deduped += 1
            history[i] = _rewrite(
                history[i],
                f"{_DEDUP_PREFIX} tool output: md5={digest}, "
                f"kept latest at #{kept}]",
            )
        return deduped

    def _soft_pass(
        self, history: list[ResponseItem], candidates: list[int]
    ) -> int:
        """soft-trim：头尾截断（truncate_middle，保头 head_chars + 尾 tail_chars）。"""
        budget = self._head_chars + self._tail_chars
        trimmed = 0
        for i in candidates:
            text = history[i].payload["output"]
            if _is_placeholder(text):
                continue  # dedup 占位符不再截断
            new_text = truncate_middle(
                text,
                budget,
                head_ratio=self._head_chars / max(budget, 1),
            )
            if new_text != text:
                trimmed += 1
                history[i] = _rewrite(history[i], new_text)
        return trimmed

    def _hard_pass(
        self, history: list[ResponseItem], candidates: list[int]
    ) -> int:
        """hard-clear：整体替换为占位符（含原始长度，LLM 可识别被剪）。"""
        cleared = 0
        for i in candidates:
            text = history[i].payload["output"]
            if _is_placeholder(text):
                continue
            cleared += 1
            history[i] = _rewrite(
                history[i],
                f"{_PRUNED_PREFIX} tool output cleared, original {len(text)} chars]",
            )
        return cleared

    # ---- 主入口 ----

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,
    ) -> CompressionResult:
        """按 ratio 分级执行三 pass，就地改写 output payload。

        窗口语义（R2）：常规窗口 = [cache_anchor_index, len - protect_tail)；
        仅 ``allow_head_clear=True`` 且 pre_turn（BEFORE_LAST_USER_MESSAGE）时
        hard-clear 可越过 anchor（跳过开头 system_injection 引导段），越过则
        如实标 ``cache_invalidated=True``。

        取消（R4）：pass 边界 ``await asyncio.sleep(0)`` 协作检查点。
        """
        history = list(ctx.history)
        names = _build_name_index(history)
        ratio = ctx.token_estimate / max(ctx.budget.context_window, 1)
        protect_from = len(history) - self._protect_tail
        detail = {"deduped": 0, "soft_trimmed": 0, "hard_cleared": 0}

        # pass 1：去重（恒启用——LLM-free 零成本），常规窗口
        normal = self._candidates(
            history, names, ctx.cache_anchor_index, protect_from
        )
        detail["deduped"] = self._dedup_pass(history, normal)
        await asyncio.sleep(0)  # 协作取消检查点

        cache_invalidated = False
        anchor_preserved = ctx.cache_anchor_index
        if ratio >= self._hard_ratio:
            # pass 3：hard-clear（取代 soft——同窗口直接清，占位符更省）
            start = ctx.cache_anchor_index
            if (
                self._allow_head_clear
                and injection == InitialContextInjection.BEFORE_LAST_USER_MESSAGE
            ):
                # 越 anchor：跳过开头 system_injection 引导段（与 sliding 同语义）
                start = 0
                while (
                    start < len(history)
                    and history[start].kind == "system_injection"
                ):
                    start += 1
            hard = self._candidates(history, names, start, protect_from)
            detail["hard_cleared"] = self._hard_pass(history, hard)
            crossed = [i for i in hard if i < ctx.cache_anchor_index]
            if detail["hard_cleared"] and crossed:
                cache_invalidated = True
                anchor_preserved = min(crossed) - 1
        elif ratio >= self._soft_ratio:
            # pass 2：soft-trim，常规窗口
            detail["soft_trimmed"] = self._soft_pass(history, normal)
        await asyncio.sleep(0)  # 协作取消检查点

        changed = sum(detail.values())
        if changed == 0:
            return CompressionResult(
                success=False,
                cache_invalidated=False,
                anchor_preserved_until=ctx.cache_anchor_index,
                reason="nothing_to_trim",
                detail=detail,
            )
        self._last_trim_at = self._clock()  # cache-TTL 闸刷新
        return CompressionResult(
            success=True,
            cache_invalidated=cache_invalidated,
            anchor_preserved_until=anchor_preserved,
            new_history=history,
            removed_item_count=changed,
            detail=detail,
        )
