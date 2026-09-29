"""audit-required Turn 的 Journal-backed Tool intent/outcome 收敛。

契约（spec「Every Tool effect has durable intent and terminal convergence」）：
① 派发前把整批 Tool 意图作为**一个原子 batch** 落 durable（有序、先于任何 runtime）；
② 每个已提交意图**恰好收敛到一个** tool_outcome_committed（success/error/rejected/
   cancelled/unknown），且在**取消无关的有界 finalization scope** 内完成；
③ 每个 call 恰好落一条 function_call_output 会话项，不重复 function_call；
④ 任一 outcome 为 UNKNOWN（无法证明外部效果是否发生）→ 记录后冻结 Session，
   在下一次效果前 fail closed。

参照 audit_llm.commit_audited_llm_response 的「durable → projection → hot history」
三段式；差异：Tool 有并发派发 + 取消窗口分类，故在 shield 内跑既有 dispatch_batch，
让工具经 ctx.cancel 协作取消产出确定结果，而 outcome 落账不被外层取消打断。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.records import (
    AttachmentV1,
    JournalIdentities,
    JournalRecordFactory,
    ToolIntentCommittedV1,
    ToolOutcomeCommittedV1,
    ToolStatus,
    conversation_item_record,
    record_id,
    stable_error,
    validate_attachments,
)
from taifeng.conversation.models import function_call_output
from taifeng.llm.errors import LLMError
from taifeng.llm.image_input import DISABLED_IMAGE_POLICY, admit_tool_attachments
from taifeng.loop.audit_support import SessionAuditFrozenError
from taifeng.suspend.reason import PendingRequest
from taifeng.tool.spec import ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from taifeng.conversation.models import ResponseItem
    from taifeng.llm.image_input import ImageInputPolicy
    from taifeng.loop.audit_bootstrap import AuditedSessionState
    from taifeng.loop.cancellation import CancellationToken
    from taifeng.loop.tool_batch import ToolCallOutcome, ToolCallRequest
    from taifeng.tool.spec import ToolSpec

# 取消/超时下「可能已产生外部效果、无法证明」的 effect 分类：需判 UNKNOWN。
# pure / idempotent 类无歧义（无外部效果 / 可安全重试），取消即 cancelled、超时即 error。
_AMBIGUOUS_EFFECT_KINDS = frozenset({"reconcilable", "external_non_idempotent"})


def _tool_effect_metadata(
    spec: ToolSpec | None,
) -> tuple[str, str | None, str]:
    """取 Tool 的 (effect_kind, idempotency_key, reconciliation)。

    spec 为 None（模型点名了本轮未提供 / 注册表也没有的工具）时按最保守分类记账：
    external_non_idempotent / manual —— 未知工具一律假设可能有不可逆外部效果。
    """
    if spec is None:
        return "external_non_idempotent", None, "manual"
    return spec.effect_kind, spec.idempotency_key, spec.reconciliation


def _classify_outcome(
    outcome: ToolCallOutcome,
    effect_kind: str,
) -> ToolStatus:
    """把一次工具执行结果归一化为稳定终态 ToolStatus。

    - suspend 非 None：声明 non-suspending 的工具在运行时挂起 → capability 违约，
      记 error 终态（调用方随后冻结）。
    - not_offered / not_in_registry / invalid_arguments：拒绝执行 → rejected。
    - cancelled / timeout：对可能有外部效果的分类无法证明是否已发生 → unknown；
      对 pure/idempotent 分别记 cancelled / error。
    - 其他 is_error → error；成功 → success。
    """
    if outcome.suspend is not None:
        return ToolStatus.ERROR
    result: ToolResult = outcome.result
    reason = result.data.get("reason") if isinstance(result.data, dict) else None
    if reason in {"not_offered", "not_in_registry", "invalid_arguments"}:
        return ToolStatus.REJECTED
    ambiguous = effect_kind in _AMBIGUOUS_EFFECT_KINDS
    if reason == "cancelled":
        return ToolStatus.UNKNOWN if ambiguous else ToolStatus.CANCELLED
    if reason == "timeout":
        return ToolStatus.UNKNOWN if ambiguous else ToolStatus.ERROR
    if result.is_error:
        return ToolStatus.ERROR
    return ToolStatus.SUCCESS


_AWAITED_REASONS = frozenset({"permission", "form", "data"})


@dataclass(frozen=True, slots=True)
class AwaitedCall:
    """一个停下等人作答的工具调用：待答请求 + 它已落账的意图。"""

    pending: Any
    intent_record_id: str


def _awaits_human(
    outcome: ToolCallOutcome, req: ToolCallRequest, spec: ToolSpec | None,
) -> bool:
    """这次调用是不是停下来等人对它作答（审批、填表、给数据）。

    - 等审批：由权限策略发起，任何工具都可能遇到；
    - 等填表 / 等数据：由工具自己发起，工具须声明 ``can_suspend=True``。

    其余情形（未声明的工具自行挂起、子 skill 挂起、失败处置、资源护栏、带到期时间的挂起）
    在审计模式下仍是能力违约。
    """
    pending = outcome.suspend
    if not isinstance(pending, PendingRequest) or pending.related_call_id != req.call_id:
        return False
    if pending.ttl_seconds is not None:
        return False
    reason = str(getattr(pending.reason, "value", pending.reason))
    if reason == "permission":
        return True
    return reason in _AWAITED_REASONS and spec is not None and spec.can_suspend is True


def _attachment_summary(attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """outcome record 里的附件摘要：类型、大小与摘要值，不含正文。"""
    if not attachments:
        return {}
    return {
        "attachments": [
            {
                "kind": item["kind"],
                "media_type": item["media_type"],
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in attachments
        ]
    }


class _AuditedToolConvergence:
    """一批 Tool call 的 durable 意图提交与取消无关的终态收敛。"""

    def __init__(
        self,
        *,
        state: AuditedSessionState,
        submission_id: str,
        turn_index: int,
        iteration: int,
        requests: Sequence[ToolCallRequest],
        registry: object,
        cancel: CancellationToken,
        origin_sample_ids: Mapping[str, str] | None = None,
        image_input_policy: ImageInputPolicy = DISABLED_IMAGE_POLICY,
    ) -> None:
        """冻结本批 identity 派生器与按 call-index 有序的请求视图。"""
        self._state = state
        self._image_input_policy = image_input_policy
        self._coordinator = state.coordinator
        self._cancel = cancel
        self._iteration = iteration
        self._turn_index = turn_index
        # requests 已按 index 升序；固定该顺序用于意图 batch 与终态收敛
        self._requests = sorted(requests, key=lambda r: r.index)
        self._registry = registry
        self._origin_sample_ids = dict(origin_sample_ids or {})
        self._identities = JournalIdentities(
            self._coordinator.session_id,
            state.thread_id,
            submission_id,
        )
        self._submission_id = submission_id
        self._turn_id = self._identities.turn(turn_index)
        self._factory = JournalRecordFactory(
            session_id=self._coordinator.session_id,
            actor=ActorRef(kind="system", source="tool"),
            identities=self._identities,
        )
        # call_id → 该 call 的 tool_intent_committed record_id（收敛时回链）
        self._intent_ids: dict[str, str] = {}
        # 本批里停下等人作答的调用（意图保持未结算）
        self.awaited: list[AwaitedCall] = []

    async def commit_intents(self) -> None:
        """派发前把整批有序 tool_intent_committed 作为一个原子 batch 落 durable。"""
        await self._coordinator.ensure_effect_allowed()
        records = []
        for req in self._requests:
            operation_id = self._identities.tool(self._turn_id, req.call_id)
            spec = self._registry.get(req.name)  # type: ignore[attr-defined]  # noqa: SLF001
            effect_kind, idempotency_key, reconciliation = _tool_effect_metadata(spec)
            record = self._factory.build(
                operation_id=operation_id,
                record_type="tool_intent_committed",
                payload=ToolIntentCommittedV1(
                    turn_index=self._turn_index,
                    iteration=self._iteration,
                    call_id=req.call_id,
                    name=req.name,
                    arguments_raw=req.arguments_raw,
                    effective_arguments=dict(req.arguments),
                    parallel_safe=req.parallel_safe,
                    effect_kind=effect_kind,
                    idempotency_key=idempotency_key,
                    reconciliation=reconciliation,
                ),
                submission_id=self._submission_id,
                thread_id=self._state.thread_id,
                turn_id=self._turn_id,
            )
            self._intent_ids[req.call_id] = record.record_id
            records.append(record)
        # 意图落账是 finalization 的一部分：即便 turn 已取消也必须先留痕，才能把每个
        # 意图收敛为 cancelled 终态；故此 append 取消无关（不传 cancel token）。
        if records:
            await self._coordinator.append_batch(tuple(records))

    async def converge(
        self,
        outcomes: Sequence[ToolCallOutcome],
    ) -> list[ResponseItem]:
        """按 call-index 有序为每个意图落 outcome + function_call_output，并推进 projection。

        任一终态为 UNKNOWN → 记录完本 call 后冻结 Session（下一次效果前 fail closed）。
        停下等人作答的调用不结算：意图保持未结算，待答请求记进 ``awaited``（ADR 0097）。
        返回供 hot history 追加的 function_call_output 会话项（与 durable 内容一致）。
        """
        by_call = {outcome.call_id: outcome for outcome in outcomes}
        fco_items: list[ResponseItem] = []
        freeze_reason: str | None = None
        for req in self._requests:
            outcome = by_call[req.call_id]
            spec = self._registry.get(req.name)  # type: ignore[attr-defined]  # noqa: SLF001
            if _awaits_human(outcome, req, spec):
                self.awaited.append(
                    AwaitedCall(outcome.suspend, self._intent_ids[req.call_id])
                )
                continue
            effect_kind, _, _ = _tool_effect_metadata(spec)
            status = _classify_outcome(outcome, effect_kind)
            fco_item = await self._commit_outcome(
                req, outcome.result, outcome.duration_ms, status
            )
            fco_items.append(fco_item)
            # 违约的挂起或 UNKNOWN 都必须在记录完终态后冻结（下一次效果前 fail closed）；
            # 挂起优先——声明 non-suspending 的工具却挂起是 capability 契约违约。
            if outcome.suspend is not None:
                freeze_reason = "audited tool suspended (capability violation)"
            elif status is ToolStatus.UNKNOWN and freeze_reason is None:
                freeze_reason = "audited tool outcome is UNKNOWN"
        if freeze_reason is not None:
            raise self._coordinator.freeze(RuntimeError(freeze_reason)) from None
        return fco_items

    async def converge_cancelled(self) -> list[ResponseItem]:
        """整批在派发前已取消：不进入 runtime，全部意图收敛为 cancelled。

        取消发生在任何 dispatch 之前 → 无外部效果、非歧义，故记 cancelled 而非
        unknown，且不冻结（取消不是失败）。
        """
        fco_items: list[ResponseItem] = []
        for req in self._requests:
            result = ToolResult.error("cancelled", reason="cancelled")
            fco_item = await self._commit_outcome(
                req, result, 0, ToolStatus.CANCELLED
            )
            fco_items.append(fco_item)
        return fco_items

    def _admitted_attachments(
        self, result: ToolResult,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """工具结果的附件过准入；返回 (可落账的附件, 拒绝说明)。

        先过图片策略（数量、MIME、尺寸），再过审计 Session 的字节上限（与用户附件同一组）。
        """
        if not result.attachments:
            return [], None
        try:
            admitted = admit_tool_attachments(result.attachments, self._image_input_policy)
            validate_attachments(
                [AttachmentV1.model_validate(item) for item in admitted],
                max_item_bytes=self._state.max_attachment_bytes,
                max_total_bytes=self._state.max_total_attachment_bytes,
            )
        except (LLMError, ValueError) as exc:
            return [], f"tool_attachment_rejected: {exc}"
        return admitted, None

    async def _commit_outcome(
        self,
        req: ToolCallRequest,
        result: ToolResult,
        duration_ms: int,
        status: ToolStatus,
    ) -> ResponseItem:
        """原子提交单个 tool_outcome_committed + 唯一 function_call_output 会话项。

        图片附件随 function_call_output 会话项落账（完整正文），outcome record 只记摘要
        （ADR 0095）。附件在落账前过准入；不合格的附件使这次调用的结果变成错误，
        与非审计路径的处置一致——不丢图，也不让脏条目进 Journal。
        """
        attachments, rejection = self._admitted_attachments(result)
        if rejection is not None:
            result = ToolResult.error(rejection, reason="tool_attachment_rejected")
            status = ToolStatus.ERROR
        operation_id = self._identities.tool(self._turn_id, req.call_id)
        intent_record_id = self._intent_ids[req.call_id]
        is_error = status is not ToolStatus.SUCCESS
        failure = stable_error(result) if is_error else None
        outcome_record = self._factory.build(
            operation_id=operation_id,
            record_type="tool_outcome_committed",
            payload=ToolOutcomeCommittedV1(
                intent_record_id=intent_record_id,
                call_id=req.call_id,
                name=req.name,
                status=status,
                output=result.output,
                data=_attachment_summary(attachments),
                duration_ms=float(duration_ms),
                stable_error=failure,
            ),
            submission_id=self._submission_id,
            thread_id=self._state.thread_id,
            turn_id=self._turn_id,
            causation_id=intent_record_id,
        )
        fco_item = function_call_output(
            call_id=req.call_id,
            output=result.output,
            thread_id=self._state.thread_id,
            is_error=is_error,
            attachments=attachments or None,
        )
        origin_sample_id = self._origin_sample_ids.get(req.call_id)
        if origin_sample_id:
            fco_item = fco_item.model_copy(update={"metadata": {
                **fco_item.metadata, "origin_llm_sample_id": origin_sample_id,
            }})
        conv_record = conversation_item_record(
            self._factory,
            operation_id=operation_id,
            item=fco_item,
            source_record_id=outcome_record.record_id,
            ordinal=0,
            submission_id=self._submission_id,
            turn_id=self._turn_id,
        )
        batch = (outcome_record, conv_record)
        ack = await self._coordinator.append_batch(batch)
        envelopes = await self._coordinator.load_acknowledged(ack, batch)
        conversation_envelopes = tuple(
            envelope for envelope in envelopes
            if envelope.record_type == "conversation_item"
        )
        try:
            projection = await self._state.projector.project(conversation_envelopes, ack)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            raise self._coordinator.freeze(error) from None
        self._coordinator.update_projection(projection)
        return fco_item


class AwaitedToolConvergence(_AuditedToolConvergence):
    """等过人的调用的结算器：意图早已落账，结果记在原来的调用名下（ADR 0097）。"""

    def __init__(self, *, intent_ids: Mapping[str, str], **kwargs: Any) -> None:
        """
        Args:
            intent_ids: 调用 id → 已落账的意图 record id。
            **kwargs: 其余同 ``_AuditedToolConvergence``；``submission_id`` / ``turn_index``
                须是这些调用**原来**所在的 turn。
        """
        super().__init__(**kwargs)
        self._intent_ids = dict(intent_ids)

    def outcome_record_id(self, request: ToolCallRequest) -> str:
        """该调用的 ``tool_outcome_committed`` 的 record id（结算前后都是它）。"""
        operation = self._identities.tool(self._turn_id, request.call_id)
        return record_id(operation, "tool_outcome_committed")

    async def settle(
        self, request: ToolCallRequest, result: ToolResult, status: ToolStatus,
    ) -> tuple[ResponseItem, str]:
        """以给定结果结算一次调用；返回 (function_call_output 对话项, outcome record id)。"""
        item = await self._commit_outcome(request, result, 0, status)
        return item, self.outcome_record_id(request)


async def audited_tool_batch(
    *,
    state: AuditedSessionState,
    submission_id: str,
    turn_index: int,
    iteration: int,
    requests: Sequence[ToolCallRequest],
    registry: object,
    run_dispatch: Callable[[], Awaitable[Sequence[ToolCallOutcome]]],
    cancel: CancellationToken,
    finalization_timeout: float,
    origin_sample_ids: Mapping[str, str] | None = None,
    image_input_policy: ImageInputPolicy = DISABLED_IMAGE_POLICY,
    awaited: list[AwaitedCall] | None = None,
) -> list[ResponseItem]:
    """audit 模式下一批 Tool 的端到端收敛：意图 → 派发 → 终态 → projection。

    ``awaited``：调用方给出的列表会被填入本批里停下等人作答的调用（ADR 0097）；不给即
    不关心——那些调用的意图保持未结算，调用方有责任随后落挂起记录。

    先原子落有序意图；随后在 shield 内跑既有 dispatch_batch —— 工具仍经 ctx.cancel
    协作取消产出确定结果，而意图收敛不被外层取消打断（取消无关的有界 finalization）。
    整批已被取消时不进入 runtime，全部意图收敛为 cancelled。
    """
    convergence = _AuditedToolConvergence(
        state=state,
        submission_id=submission_id,
        turn_index=turn_index,
        iteration=iteration,
        requests=requests,
        registry=registry,
        cancel=cancel,
        origin_sample_ids=origin_sample_ids,
        image_input_policy=image_input_policy,
    )
    # 取消无关的有界 finalization：意图落账 + 收敛都在 shield 内，无论外层取消与否
    # 每个已提交意图都必须收敛出唯一终态。
    # fail-closed：finalization 到期（TimeoutError）或收敛内任何非取消异常逃逸时，
    # 意图已 durable 但未必都收敛出终态 → 必须冻结 Session（下一次效果前拒绝），
    # 绝不 fail-open（否则会留下无 outcome 的 dangling intent 且 Session 仍接受效果）。
    try:
        with anyio.fail_after(finalization_timeout, shield=True):
            await convergence.commit_intents()
            # 整批在任何 dispatch 之前已取消 → 不进入 runtime，全部收敛为 cancelled
            # （区别于 dispatch 中途取消的 ambiguous → unknown）
            if cancel.is_cancelled:
                return await convergence.converge_cancelled()
            outcomes = await run_dispatch()
            items = await convergence.converge(outcomes)
            if awaited is not None:
                awaited.extend(convergence.awaited)
            elif convergence.awaited:
                raise RuntimeError("audited tool suspended without a suspension owner")
            return items
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except SessionAuditFrozenError:
        # converge/commit 内已 fail closed（UNKNOWN/挂起/Journal 失败）→ 原样上抛
        raise
    except BaseException as error:
        # 含 finalization TimeoutError、缺分支 KeyError、intent append IO 失败等
        raise state.coordinator.freeze(error) from None


async def run_audited_tools(
    runner: Any,
    *,
    iteration: int,
    requests: Sequence[ToolCallRequest],
    run_dispatch: Callable[[], Awaitable[Sequence[ToolCallOutcome]]],
    origin_sample_ids: Mapping[str, str] | None,
) -> None:
    """turn 的审计工具批：结算的结果进 hot history；有调用在等人则让 turn 挂起。

    Raises:
        _BatchSuspend: 本批有调用停下等人作答（已结算的结果已进 hot history）。
    """
    awaited: list[AwaitedCall] = []
    state = runner.audit_state
    items = await audited_tool_batch(
        state=state,
        submission_id=runner.submission_id,
        turn_index=runner.turn_index,
        iteration=iteration,
        requests=requests,
        registry=runner.tool_runtime._registry,  # noqa: SLF001
        run_dispatch=run_dispatch,
        cancel=runner.cancel,
        finalization_timeout=state.coordinator.finalization_timeout,
        origin_sample_ids=origin_sample_ids,
        image_input_policy=runner.image_input_policy,
        awaited=awaited,
    )
    runner.history_buffer.extend(items)
    if not awaited:
        return
    # 挂起落账时要回指这些调用的意图
    runner._persist.awaited_intents = {  # noqa: SLF001
        call.pending.related_call_id: call.intent_record_id for call in awaited
    }
    from taifeng.loop import turn as _turn_mod

    raise _turn_mod._BatchSuspend(tuple(call.pending for call in awaited))  # noqa: SLF001


__all__ = [
    "AwaitedCall",
    "AwaitedToolConvergence",
    "audited_tool_batch",
    "run_audited_tools",
]
