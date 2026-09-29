"""冷恢复时收敛在飞工具调用 —— 按副作用类型分流（tool-crash-reconciliation）。

进程在工具执行途中崩溃时，transcript 里留下「有调用、无结果」的悬空调用：

- Chat 协议路径：只有派发前落的 ``tool_intent``（function_call 要等执行完才成对落盘）；
- Responses 协议路径：function_call 已随最终响应原子落盘，缺 function_call_output。

此前 Chat 路径崩溃后意图整个丢失（resume 后模型不知情地重发 → 副作用静默重复），
Responses 路径则一刀切回填「结果未知，不重试」。本模块按工具声明的副作用类型分流：

| 工具声明 | 处置 |
| --- | --- |
| ``effect_kind`` 为 ``pure`` / ``idempotent`` | 回填「中断、可安全重发」，模型自行决定是否重调 |
| 提供了 ``reconcile`` 回查函数 | 调它查清真实结局：已完成 → 回填真实结果；未执行 → 同上「可安全重发」；查不清 → 交人 |
| 其余（``external_non_idempotent`` / 未注册工具 / 回查失败） | 落 ``TOOL_OUTCOME_UNKNOWN`` 挂起交人裁决（``mode="report"`` 时退回旧行为：回填「结果未知，不重试」） |

参照：ADR 0025 恢复语义（未匹配 intent 一律 UNKNOWN，仅幂等或 reconciler 证明后才重试 /
补写结果）；claw-code 的 recovery recipe。差异：非审计路径不冻结会话，而是复用既有挂起 /
Resume 机制把裁决权交给人。

幂等：所有补写项 id 由 (thread / sample, call_id) 确定性派生；恢复途中再崩溃，下次恢复时
已补写的调用不再悬空，已挂起的调用由活跃挂起集合排除，不会重复处置。

strict audit 会话不走本模块的落盘路径（Journal 自有 UNKNOWN 语义，见 ``audit_resume_tools``），
但复用这里的分流原语：可安全重发的副作用集合、回填文案、回查执行与确定性 id 派生
（``RETRY_SAFE_EFFECTS`` / ``safe_to_retry_text`` / ``run_reconcile`` / ``stable_recovery_id``）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from taifeng.conversation.models import ResponseItem, function_call
from taifeng.loop.tool_batch import parse_tool_arguments
from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.tool.spec import ReconcileVerdict

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from taifeng.conversation.store import MessageStore
    from taifeng.tool.registry import ToolRegistry
    from taifeng.tool.spec import ToolSpec

logger = logging.getLogger(__name__)

ToolRecoveryMode = Literal["suspend", "report"]
"""非幂等且无法回查的悬空调用怎么处置：挂起交人（默认）/ 回填「结果未知」（旧行为）。"""

Disposition = Literal[
    "safe_to_retry", "reconciled", "awaiting_operator", "reported_unknown", "operator_resolved",
    "not_dispatched", "dispatch_interrupted",
]
"""悬空调用的处置结论。``operator_resolved`` / ``not_dispatched`` / ``dispatch_interrupted`` 只出现
在 strict audit resume：人的裁决经 ``AuditConfig.tool_outcome_resolver`` 在接管时提交（审计会话
不能挂起，见 ADR 0070）；调用从未登记意图或派发从未启动、确定未执行（ADR 0075 / 0076）；
``call_skill`` 派发的子 skill 在执行途中被中断（ADR 0076）。"""

RETRY_SAFE_EFFECTS = frozenset({"pure", "idempotent"})
"""可在不知结局时安全重发的副作用分类。"""

_SAFE_TO_RETRY_TEXT = (
    "tool call interrupted by process recovery before its result was recorded; "
    "the tool is declared {effect} and may be called again safely"
)
NOT_EXECUTED_TEXT = (
    "tool call interrupted by process recovery; reconciliation confirmed it was "
    "not executed, so it may be called again safely"
)
"""回查确认未执行时回填给模型的文本（is_error=True）。"""

NOT_DISPATCHED_TEXT = (
    "not_executed: the process stopped before this tool call was registered for "
    "dispatch, so it never ran and may be called again safely"
)
"""调用从未登记意图（确定未执行）时回填给模型的文本（is_error=True）。"""

DISPATCH_NOT_STARTED_TEXT = (
    "not_executed: the process stopped before the skill dispatch started, so the "
    "skill never ran and may be called again safely"
)
"""``call_skill`` 的派发从未启动（确定未执行）时回填给模型的文本（is_error=True）。"""

OPERATOR_ABORTED_TEXT = (
    "tool_outcome_unknown: aborted by operator after crash recovery; not retried"
)
"""人裁决放弃查明结局、接受未知并继续时回填的文本（前缀不暗示「确定没执行」）。"""

_UNKNOWN_TEXT = "tool outcome unknown after process recovery; not retried"


def dispatch_interrupted_text(skill_id: str, settled: Sequence[RecoveredCall]) -> str:
    """子 skill 在执行途中被中断时回填给父调用的文本（is_error=True）。

    逐条列出子 skill 内已收敛调用的处置结论：模型据此判断重新派发会不会重复副作用。
    """
    if settled:
        calls = "; ".join(f"{c.name} ({c.call_id}): {c.disposition}" for c in settled)
        detail = f"Tool calls inside it were settled as: {calls}. "
    else:
        detail = "No tool call inside it was left unsettled. "
    return (
        f"skill_dispatch_interrupted: skill {skill_id!r} was interrupted by process "
        "recovery before it finished, and its partial work was not returned. "
        f"{detail}"
        "The skill may be called again; review the settled calls before repeating side effects."
    )


def safe_to_retry_text(effect_kind: str) -> str:
    """副作用声明为可安全重发时回填给模型的文本（is_error=True）。"""
    return _SAFE_TO_RETRY_TEXT.format(effect=effect_kind)


@dataclass(frozen=True)
class DanglingCall:
    """一个有调用、无结果的悬空工具调用。"""

    call_id: str
    name: str
    arguments: str
    thread_id: str
    has_function_call: bool
    """transcript 里是否已有 function_call（Responses 路径 / Chat 成对写到一半）。"""
    llm_sample_id: str | None
    """Responses 路径的采样 id（补写结果须按采样原子批落盘）；Chat 路径为 None。"""
    extra_content: dict[str, object] | None = None


@dataclass(frozen=True)
class RecoveredCall:
    """一个悬空调用的处置结论（随 ``thread_resumed`` 事件透出）。"""

    call_id: str
    name: str
    disposition: Disposition

    def as_dict(self) -> dict[str, str]:
        """JSON 友好视图。"""
        return {"call_id": self.call_id, "name": self.name, "disposition": self.disposition}


def validate_tool_recovery_mode(mode: str) -> ToolRecoveryMode:
    """构造期校验恢复模式（非法值显式报错，禁静默回退）。

    Raises:
        ValueError: mode 不是 ``suspend`` / ``report``。
    """
    if mode == "suspend":
        return "suspend"
    if mode == "report":
        return "report"
    raise ValueError(f"tool_recovery must be 'suspend' or 'report', got {mode!r}")


def active_suspension_call_ids(history: tuple[ResponseItem, ...]) -> set[str]:
    """从 append-only marker 推导仍由挂起记录持有的 call ids（这些不算悬空）。"""
    resolved: set[str] = set()
    suspensions: list[ResponseItem] = []
    for item in history:
        if item.kind == "system_injection" and item.payload.get("source") == "suspend_resolved":
            resolved.add(str(item.payload.get("text", "")).removeprefix("suspend_resolved:"))
        elif item.kind == "suspension":
            suspensions.append(item)
    active: set[str] = set()
    for item in suspensions:
        if item.payload.get("record_id") in resolved:
            continue
        for pending in item.payload.get("pending", []):
            call_id = pending.get("related_call_id") if isinstance(pending, dict) else None
            if isinstance(call_id, str) and call_id:
                active.add(call_id)
    return active


def find_dangling_calls(history: tuple[ResponseItem, ...]) -> list[DanglingCall]:
    """按首次出现序找出所有悬空调用（有 intent / function_call、无 output、不在活跃挂起中）。"""
    completed = {
        str(item.payload.get("call_id"))
        for item in history
        if item.kind == "function_call_output" and item.payload.get("call_id")
    }
    blocked = completed | active_suspension_call_ids(history)
    with_fc = {
        str(item.payload.get("call_id"))
        for item in history
        if item.kind == "function_call" and item.payload.get("call_id")
    }
    seen: set[str] = set()
    dangling: list[DanglingCall] = []
    for item in history:
        if item.kind not in ("function_call", "tool_intent"):
            continue
        call_id = item.payload.get("call_id")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            continue
        seen.add(call_id)
        if call_id in blocked:
            continue
        sample_id = item.metadata.get("llm_sample_id")
        extra = item.payload.get("extra_content")
        dangling.append(DanglingCall(
            call_id=call_id,
            name=str(item.payload.get("name", "")),
            arguments=str(item.payload.get("arguments", "")),
            thread_id=item.thread_id,
            has_function_call=call_id in with_fc,
            llm_sample_id=sample_id if isinstance(sample_id, str) and sample_id else None,
            extra_content=extra if isinstance(extra, dict) else None,
        ))
    return dangling


async def run_reconcile(
    spec: ToolSpec, arguments: dict[str, Any], call_id: str,
) -> ReconcileVerdict | None:
    """调工具的回查函数；抛异常 / 超时 / 返回值类型不对一律视为查不清（返回 None）。

    回查受 ``spec.timeout_seconds`` 限时（R4）。失败不阻断恢复，由调用方降级为交人裁决
    （有日志，非静默）。

    Args:
        spec: 提供了 ``reconcile`` 的工具声明。
        arguments: 该次调用的参数对象。
        call_id: 待回查的调用 id。
    """
    assert spec.reconcile is not None
    try:
        verdict = await asyncio.wait_for(
            spec.reconcile(arguments, call_id), timeout=spec.timeout_seconds)
    except Exception:
        # 回查是恢复的辅助手段：失败不阻断恢复，降级为交人裁决（有日志，非静默）
        logger.exception("tool reconcile failed for %s (%s)", spec.name, call_id)
        return None
    if not isinstance(verdict, ReconcileVerdict):
        # 业务回查函数违约：同样视为查不清，不猜测其含义
        logger.error("tool reconcile for %s (%s) returned %s, not ReconcileVerdict",
                     spec.name, call_id, type(verdict).__name__)
        return None
    return verdict


async def _reconcile_dangling(spec: ToolSpec, call: DanglingCall) -> ReconcileVerdict | None:
    """非审计路径：参数取自 transcript 原始串；参数非法即视为查不清。"""
    arguments, error = parse_tool_arguments(call.arguments)
    if error is not None:
        return None
    return await run_reconcile(spec, arguments, call.call_id)


async def _decide(
    call: DanglingCall, spec: ToolSpec | None, mode: ToolRecoveryMode,
) -> tuple[Disposition, str | None, bool]:
    """给出一个悬空调用的处置：(disposition, 回填文本 | None=挂起, is_error)。"""
    if spec is not None and spec.reconcile is not None:
        verdict = await _reconcile_dangling(spec, call)
        if verdict is not None and verdict.status == "completed":
            return "reconciled", verdict.output, verdict.is_error
        if verdict is not None and verdict.status == "not_executed":
            return "safe_to_retry", NOT_EXECUTED_TEXT, True
    elif spec is not None and spec.effect_kind in RETRY_SAFE_EFFECTS:
        return "safe_to_retry", safe_to_retry_text(spec.effect_kind), True
    if mode == "report":
        return "reported_unknown", _UNKNOWN_TEXT, True
    return "awaiting_operator", None, True


def stable_recovery_id(prefix: str, *parts: str) -> str:
    """由恢复语境派生确定性 id（恢复幂等的基础）。"""
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _recovery_output(call: DanglingCall, text: str, is_error: bool) -> ResponseItem:
    """构造恢复补写的 function_call_output（Responses 路径带采样归属元数据）。"""
    scope = call.llm_sample_id or call.thread_id
    metadata = {"origin_llm_sample_id": call.llm_sample_id} if call.llm_sample_id else {}
    return ResponseItem(
        kind="function_call_output",
        id=stable_recovery_id("item_recovery", scope, call.call_id),
        thread_id=call.thread_id,
        payload={"call_id": call.call_id, "output": text, "is_error": is_error},
        metadata={**metadata, "recovered": True},
    )


def _operator_pending(call: DanglingCall, spec: ToolSpec | None) -> PendingRequest:
    """构造交人裁决的挂起请求（detail 供业务渲染裁决 UI）。"""
    return PendingRequest(
        request_id=stable_recovery_id("req_recovery", call.thread_id, call.call_id),
        reason=SuspendReason.TOOL_OUTCOME_UNKNOWN,
        payload_schema={
            "type": "object",
            "properties": {
                "action": {"enum": ["retry", "provide", "abort"]},
                "output": {"type": "string"},
                "is_error": {"type": "boolean"},
            },
            "required": ["action"],
        },
        related_call_id=call.call_id,
        detail={
            "tool": call.name,
            "arguments": call.arguments,
            "effect_kind": spec.effect_kind if spec is not None else None,
            "reconciliation": spec.reconciliation if spec is not None else None,
            "registered": spec is not None,
        },
    )


async def recover_dangling_tool_calls(
    store: MessageStore,
    history: tuple[ResponseItem, ...],
    registry: ToolRegistry,
    *,
    mode: ToolRecoveryMode = "suspend",
    now: Callable[[], int],
) -> tuple[tuple[ResponseItem, ...], tuple[RecoveredCall, ...]]:
    """收敛全部悬空调用并重载 durable history。

    落盘顺序：先为只有 intent 的 Chat 调用补 function_call（与意图同参），再写回填
    output（Responses 按采样原子批），最后把需要人裁决的调用合并成一条挂起记录。

    Args:
        store: 目标 thread 所在 store。
        history: 冷加载得到的逻辑 history。
        registry: 工具注册表（查副作用声明与回查函数）。
        mode: 非幂等且无法回查时的处置方式。
        now: 挂起记录时间戳工厂（R1：内核不自取时钟）。

    Returns:
        (重载后的 history, 各悬空调用的处置结论)；无悬空调用时原样返回。

    Raises:
        TypeError: 存在 Responses 采样的回填，但 store 不支持原子批写。
    """
    dangling = find_dangling_calls(history)
    if not dangling:
        return history, ()
    thread_id = history[0].thread_id
    recovered: list[RecoveredCall] = []
    outputs_by_sample: dict[str, list[ResponseItem]] = {}
    plain_items: list[ResponseItem] = []
    operator: list[PendingRequest] = []
    for call in dangling:
        spec = registry.get(call.name)
        disposition, text, is_error = await _decide(call, spec, mode)
        recovered.append(RecoveredCall(call.call_id, call.name, disposition))
        # Chat 路径只有意图：先补 function_call，使 output / 挂起都有配对对象
        if not call.has_function_call:
            fc = function_call(
                call.call_id, call.name, call.arguments,
                thread_id=call.thread_id, extra_content=call.extra_content,
            ).model_copy(update={
                "id": stable_recovery_id("item_recovery_fc", call.thread_id, call.call_id),
            })
            plain_items.append(fc)
        if text is None:
            operator.append(_operator_pending(call, spec))
        elif call.llm_sample_id is not None:
            outputs_by_sample.setdefault(call.llm_sample_id, []).append(
                _recovery_output(call, text, is_error))
        else:
            plain_items.append(_recovery_output(call, text, is_error))
    await _persist_recovery(store, plain_items, outputs_by_sample)
    if operator:
        record = SuspensionRecord(
            record_id=stable_recovery_id("rec_recovery", thread_id, *(p.request_id for p in operator)),
            thread_id=thread_id,
            submission_id="recovery",
            turn_index=0,
            pending=tuple(operator),
            created_at=now(),
        )
        await store.append(record.to_item())
    iterator = await store.load_thread(thread_id)
    reloaded = tuple([item async for item in iterator])
    return reloaded, tuple(recovered)


async def _persist_recovery(
    store: MessageStore,
    plain_items: list[ResponseItem],
    outputs_by_sample: dict[str, list[ResponseItem]],
) -> None:
    """落盘补写项：Chat 逐条追加；Responses 按采样原子批（与正常采样同一持久化语义）。"""
    for item in plain_items:
        await store.append(item)
    if not outputs_by_sample:
        return
    from taifeng.conversation.store import AtomicBatchMessageStore

    if not isinstance(store, AtomicBatchMessageStore):
        raise TypeError("Responses recovery requires AtomicBatchMessageStore")
    for sample_id, outputs in outputs_by_sample.items():
        digest = hashlib.sha256(sample_id.encode()).hexdigest()[:24]
        await store.append_atomic_batch(
            outputs, batch_id=f"recovery:unknown_tool_outcome:{digest}")


__all__ = [
    "NOT_EXECUTED_TEXT",
    "OPERATOR_ABORTED_TEXT",
    "RETRY_SAFE_EFFECTS",
    "DanglingCall",
    "Disposition",
    "RecoveredCall",
    "ToolRecoveryMode",
    "active_suspension_call_ids",
    "find_dangling_calls",
    "recover_dangling_tool_calls",
    "run_reconcile",
    "safe_to_retry_text",
    "stable_recovery_id",
    "validate_tool_recovery_mode",
]
