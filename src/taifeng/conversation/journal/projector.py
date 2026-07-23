"""把已 durable ack 的 conversation_item 投影到可重建 MessageStore。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from taifeng.conversation.journal.records import (
    ConversationItemV1,
    deserialize_response_item,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractAsyncContextManager

    from taifeng.conversation.journal.materialization import (
        ProjectionFileIdentity,
        ProjectionReplayWindow,
        ProjectionSnapshot,
    )
    from taifeng.conversation.journal.models import JournalAck, JournalEnvelope
    from taifeng.conversation.models import ResponseItem


class ProjectionOrderError(ValueError):
    """Journal 顺序、ack 覆盖或 conversation wire contract 不成立。"""


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    """单 thread materialization 的可观察状态。"""

    thread_id: str
    projected_seq: int
    stale: bool
    failure_class: str | None = None
    failure_record_id: str | None = None

    def __post_init__(self) -> None:
        """拒绝无法表达可信 projector 结果的类型、负水位与 failure 组合。"""
        if not isinstance(self.thread_id, str) or not self.thread_id:
            raise ValueError("projection thread_id must be a non-empty string")
        if (
            isinstance(self.projected_seq, bool)
            or not isinstance(self.projected_seq, int)
            or self.projected_seq < 0
        ):
            raise ValueError("projection projected_seq must be a non-negative integer")
        if not isinstance(self.stale, bool):
            raise ValueError("projection stale must be a boolean")
        if self.failure_record_id is not None and (
            not isinstance(self.failure_record_id, str) or not self.failure_record_id
        ):
            raise ValueError("projection failure_record_id must be non-empty when present")
        if self.stale:
            if not isinstance(self.failure_class, str) or not self.failure_class:
                raise ValueError("stale projection requires a non-empty failure_class")
        elif self.failure_class is not None or self.failure_record_id is not None:
            raise ValueError("healthy projection cannot carry failure fields")


class ConversationProjectionStore(Protocol):
    """Journal projector 所需的最窄 transcript store 协议。"""

    async def create_projection_thread(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        entry_skill_id: str,
        source: str,
        extra: dict[str, Any],
    ) -> str:
        """用调用方预分配 id 创建空投影。"""
        ...

    def projection_scope(self, thread_id: str) -> AbstractAsyncContextManager[None]:
        """准入 store handle 并持有物理 target 的 per-thread 锁。"""
        ...

    def projection_state(
        self, thread_id: str
    ) -> tuple[ProjectionResult | None, int | None]:
        """读取 store-owned result 与 blocked seq。"""
        ...

    async def expected_projection_session_id(self, thread_id: str) -> str:
        """返回 physical target/bootstrap 绑定的 Journal Session identity。"""
        ...

    def update_projection_state(
        self,
        thread_id: str,
        result: ProjectionResult,
        blocked_seq: int | None,
    ) -> None:
        """在共享 thread 锁内更新 store-owned state。"""
        ...

    def projection_first_seq(self, thread_id: str) -> int | None:
        """返回物理 target 首次健康观察的 conversation seq。"""
        ...

    def record_projection_first_seq(self, thread_id: str, seq: int) -> None:
        """首次健康投影后记录 generation replay 起点。"""
        ...

    def projection_replay_window(self, thread_id: str) -> ProjectionReplayWindow | None:
        """返回 physical target 当前 generation replay 窗口。"""
        ...

    def advance_projection_replay(self, thread_id: str, observed_seq: int) -> None:
        """推进 generation replay，追平旧 watermark 后清理窗口。"""
        ...

    async def load_projection_snapshot(self, thread_id: str) -> ProjectionSnapshot:
        """加载已验证 metadata 的 cache-aware snapshot。"""
        ...

    async def append_projection_batch(
        self,
        thread_id: str,
        items: list[ResponseItem],
        expected_identity: ProjectionFileIdentity,
    ) -> None:
        """按 snapshot identity 追加 conversation items。"""
        ...


@dataclass(frozen=True, slots=True)
class _ProjectedEnvelope:
    """已经完整验证、可安全物化的 envelope。"""

    seq: int
    record_id: str
    item: ResponseItem


@dataclass(frozen=True, slots=True)
class _ProjectionConflict:
    """历史核对时发现的稳定 stale 分类与责任 record。"""

    failure_class: str
    record_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class _MaterializationPlan:
    """历史核对成功后需要追加的 suffix 及其起始下标。"""

    first_missing: int
    items: tuple[ResponseItem, ...]


def _validate_audit_metadata(extra: dict[str, Any]) -> None:
    """要求调用方显式提供完整 audited transcript marker。"""
    session_id = extra.get("journal_session_id")
    schema_version = extra.get("journal_schema_version")
    if (
        extra.get("audit_required") is not True
        or not isinstance(session_id, str)
        or not session_id
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise ProjectionOrderError("complete audit metadata is required")


def _validate_envelope(envelope: JournalEnvelope, ack: JournalAck) -> _ProjectedEnvelope:
    """验证单条 envelope 受 ack 覆盖且 payload 是显式 ConversationItemV1。"""
    if envelope.record_type != "conversation_item":
        raise ProjectionOrderError("only conversation_item envelopes may be projected")
    if envelope.session_id != ack.session_id:
        raise ProjectionOrderError("envelope session does not match ack")
    if envelope.writer_epoch != ack.writer_epoch:
        raise ProjectionOrderError("envelope writer epoch does not match ack")
    if not ack.first_seq <= envelope.seq <= ack.last_seq:
        raise ProjectionOrderError("envelope seq is not covered by ack")
    if envelope.record_id not in ack.record_ids:
        raise ProjectionOrderError("envelope record id is not covered by ack")
    try:
        payload = ConversationItemV1.model_validate(envelope.payload)
        item = deserialize_response_item(payload)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ProjectionOrderError("invalid ConversationItemV1 payload") from exc
    if envelope.thread_id != payload.thread_id or item.thread_id != payload.thread_id:
        raise ProjectionOrderError("envelope and item thread ids do not match")
    return _ProjectedEnvelope(seq=envelope.seq, record_id=envelope.record_id, item=item)


def _validate_ack(ack: JournalAck) -> None:
    """验证 ack 覆盖连续范围且 record id 唯一。"""
    if ack.first_seq > ack.last_seq:
        raise ProjectionOrderError("ack sequence range is invalid")
    expected_record_count = ack.last_seq - ack.first_seq + 1
    if len(ack.record_ids) != expected_record_count:
        raise ProjectionOrderError("ack record ids must cover its complete sequence range")
    if len(set(ack.record_ids)) != len(ack.record_ids):
        raise ProjectionOrderError("ack record ids must be unique")


def _project_envelopes(
    envelopes: Sequence[JournalEnvelope],
    ack: JournalAck,
) -> tuple[_ProjectedEnvelope, ...]:
    """逐条验证 Journal 顺序、ack offset 与 item 唯一性。"""
    projected: list[_ProjectedEnvelope] = []
    seen_records: set[str] = set()
    seen_items: set[str] = set()
    previous_seq = 0
    for envelope in envelopes:
        if envelope.seq <= previous_seq:
            raise ProjectionOrderError("envelope seq must be strictly increasing")
        if envelope.record_id in seen_records:
            raise ProjectionOrderError("envelope record ids must be unique")
        entry = _validate_envelope(envelope, ack)
        if ack.record_ids[envelope.seq - ack.first_seq] != envelope.record_id:
            raise ProjectionOrderError("envelope order does not match ack record order")
        if entry.item.id in seen_items:
            raise ProjectionOrderError("conversation item ids must be unique")
        projected.append(entry)
        previous_seq = envelope.seq
        seen_records.add(envelope.record_id)
        seen_items.add(entry.item.id)
    return tuple(projected)


def _validate_batch(
    envelopes: Sequence[JournalEnvelope], ack: JournalAck
) -> tuple[_ProjectedEnvelope, ...]:
    """在任何 store write 前验证完整单 thread batch。"""
    if not envelopes:
        raise ProjectionOrderError("projection batch must not be empty")
    _validate_ack(ack)
    projected = _project_envelopes(envelopes, ack)
    thread_ids = {entry.item.thread_id for entry in projected}
    if len(thread_ids) != 1:
        raise ProjectionOrderError("cross-thread projection batches are not supported")
    return projected


def _content_conflict(
    history: list[ResponseItem],
    projected: tuple[_ProjectedEnvelope, ...],
) -> _ProjectionConflict | None:
    """检测 stored id 重复或同 id 不同内容。"""
    existing = {item.id: item for item in history}
    if len(existing) != len(history):
        first = projected[0]
        return _ProjectionConflict("duplicate_stored_item_id", first.record_id, first.seq)
    for entry in projected:
        stored = existing.get(entry.item.id)
        if stored is not None and stored != entry.item:
            return _ProjectionConflict("item_id_conflict", entry.record_id, entry.seq)
    return None


def _order_plan(
    history: list[ResponseItem],
    projected: tuple[_ProjectedEnvelope, ...],
) -> _MaterializationPlan | _ProjectionConflict:
    """验证已存在 items 连续且仅允许缺失 suffix。"""
    positions = {item.id: index for index, item in enumerate(history)}
    present = [entry.item.id in positions for entry in projected]
    projected_positions = [
        positions[entry.item.id]
        for entry, is_present in zip(projected, present, strict=True)
        if is_present
    ]
    first_missing = next(
        (index for index, value in enumerate(present) if not value),
        len(present),
    )
    ordered = all(
        right == left + 1
        for left, right in zip(projected_positions, projected_positions[1:], strict=False)
    )
    suffix_follows_tail = not (
        first_missing < len(projected)
        and projected_positions
        and projected_positions[-1] < len(history) - 1
    )
    if not ordered or not suffix_follows_tail or any(present[first_missing:]):
        entry = projected[first_missing] if first_missing < len(projected) else projected[0]
        return _ProjectionConflict("projection_order_conflict", entry.record_id, entry.seq)
    return _MaterializationPlan(
        first_missing=first_missing,
        items=tuple(entry.item for entry in projected[first_missing:]),
    )


def _plan_materialization(
    history: list[ResponseItem],
    projected: tuple[_ProjectedEnvelope, ...],
) -> _MaterializationPlan | _ProjectionConflict:
    """把内容与顺序核对组合成一个无副作用 materialization plan。"""
    conflict = _content_conflict(history, projected)
    return conflict if conflict is not None else _order_plan(history, projected)


def _validate_generation_prefix(
    history: list[ResponseItem],
    plan: _MaterializationPlan,
    projected: tuple[_ProjectedEnvelope, ...],
    window: ProjectionReplayWindow,
) -> _ProjectionConflict | None:
    """要求本批写后的 history 仍是旧 healthy snapshot 的完整前缀。"""
    recovered = (*history, *plan.items)
    expected = window.expected_items
    prefix_matches = len(recovered) <= len(expected) and recovered == expected[: len(recovered)]
    complete_at_ceiling = projected[-1].seq < window.ceiling or recovered == expected
    if prefix_matches and complete_at_ceiling:
        return None
    entry = projected[plan.first_missing] if plan.first_missing < len(projected) else projected[0]
    return _ProjectionConflict(
        "generation_replay_mismatch",
        entry.record_id,
        entry.seq,
    )


def _validated_materialization_plan(
    history: list[ResponseItem],
    projected: tuple[_ProjectedEnvelope, ...],
    window: ProjectionReplayWindow | None,
) -> _MaterializationPlan | _ProjectionConflict:
    """组合普通物化与 generation expected-prefix 校验。"""
    plan = _plan_materialization(history, projected)
    if isinstance(plan, _ProjectionConflict):
        if window is None:
            return plan
        return _ProjectionConflict(
            "generation_replay_mismatch",
            plan.record_id,
            plan.seq,
        )
    if window is None:
        return plan
    return _validate_generation_prefix(history, plan, projected, window) or plan


def _materialized_seq(
    current: ProjectionResult,
    window: ProjectionReplayWindow | None,
) -> int:
    """generation replay 中只报告本 generation 已验证的实际进度。"""
    return current.projected_seq if window is None else window.progress or 0


def _is_generation_replay(
    projected: tuple[_ProjectedEnvelope, ...],
    window: ProjectionReplayWindow | None,
) -> bool:
    """只允许 replay 窗口从 floor 开始并持续单调推进到 ceiling。"""
    if window is None or projected[-1].seq > window.ceiling:
        return False
    if window.progress is None:
        return projected[0].seq == window.floor
    return projected[0].seq > window.progress


class JournalConversationProjector:
    """只把 covering durable JournalAck 对应的 conversation envelopes 投影到 store。

    每次调用只接受一个 thread；ack 可覆盖同 batch 中未传入的领域 records，因此 seq gap 合法。
    watermark 是物理 materialization target 的 per-thread 状态，target 最后一个 handle 关闭后释放；
    item 去重通过 identity-aware snapshot cache 校验，新 projector 与部分失败后仍可收敛。
    """

    def __init__(self, store: ConversationProjectionStore) -> None:
        self._store = store

    async def bootstrap_thread(
        self,
        *,
        thread_id: str,
        cwd: str | None,
        entry_skill_id: str,
        source: str,
        extra: dict[str, Any],
    ) -> str:
        """校验调用方审计 marker 后创建预分配 id 的空投影。"""
        _validate_audit_metadata(extra)
        return await self._store.create_projection_thread(
            thread_id=thread_id,
            cwd=cwd,
            entry_skill_id=entry_skill_id,
            source=source,
            extra=extra,
        )

    def state(self, thread_id: str) -> ProjectionResult:
        """返回 thread 当前 watermark/stale 快照。"""
        result, _ = self._store.projection_state(thread_id)
        if result is not None:
            return result
        return ProjectionResult(thread_id=thread_id, projected_seq=0, stale=False)

    async def project(
        self,
        envelopes: Sequence[JournalEnvelope],
        ack: JournalAck,
    ) -> ProjectionResult:
        """验证完整 batch 后按 Journal seq 物化；store 失败只返回 stale。"""
        projected = _validate_batch(envelopes, ack)
        thread_id = projected[0].item.thread_id
        async with self._store.projection_scope(thread_id):
            await self._validate_projection_session(thread_id, ack.session_id)
            return await self._materialize(thread_id, projected)

    async def _validate_projection_session(
        self,
        thread_id: str,
        session_id: str,
    ) -> None:
        """在 snapshot repair/write 前验证 transcript 的 Journal Session 绑定。"""
        try:
            expected = await self._store.expected_projection_session_id(thread_id)
        except Exception as exc:  # noqa: BLE001  # identity 缺失必须稳定 fail closed
            raise ProjectionOrderError(
                "expected Journal Session identity is unavailable"
            ) from exc
        if expected != session_id:
            raise ProjectionOrderError(
                "batch Journal Session does not match transcript identity"
            )

    async def _materialize(
        self,
        thread_id: str,
        projected: tuple[_ProjectedEnvelope, ...],
    ) -> ProjectionResult:
        """在 per-thread 锁内核对 durable history、去重并追加缺失 suffix。"""
        current = self.state(thread_id)
        _, blocked_seq = self._store.projection_state(thread_id)
        if blocked_seq is not None and all(entry.seq != blocked_seq for entry in projected):
            return current
        first_missing = 0
        replay_window: ProjectionReplayWindow | None = None
        try:
            snapshot = await self._store.load_projection_snapshot(thread_id)
            replay_window = self._store.projection_replay_window(thread_id)
            actual_seq = _materialized_seq(current, replay_window)
            invalid_replay = (
                not _is_generation_replay(projected, replay_window)
                if replay_window is not None
                else projected[-1].seq < current.projected_seq
            )
            if invalid_replay:
                return self._set_stale(
                    thread_id,
                    actual_seq,
                    "sequence_regression",
                    projected[0].record_id,
                    projected[0].seq,
                )
            history = list(snapshot.items)
            plan = _validated_materialization_plan(history, projected, replay_window)
            if isinstance(plan, _ProjectionConflict):
                return self._set_stale(
                    thread_id,
                    actual_seq,
                    plan.failure_class,
                    plan.record_id,
                    plan.seq,
                )
            first_missing = plan.first_missing
            if plan.items:
                await self._store.append_projection_batch(
                    thread_id,
                    list(plan.items),
                    snapshot.identity,
                )
        except Exception as exc:  # noqa: BLE001  # materialization 不得冻结 Journal
            return self._set_stale(
                thread_id,
                _materialized_seq(current, replay_window),
                type(exc).__name__,
                projected[first_missing].record_id if first_missing < len(projected) else None,
                (
                    projected[first_missing].seq
                    if first_missing < len(projected)
                    else projected[0].seq
                ),
            )
        return self._set_healthy(
            thread_id,
            current,
            projected[0].seq,
            projected[-1].seq,
        )

    def _set_healthy(
        self,
        thread_id: str,
        current: ProjectionResult,
        observed_first_seq: int,
        observed_last_seq: int,
    ) -> ProjectionResult:
        """保存并返回 materialization 健康 watermark。"""
        replay_window = self._store.projection_replay_window(thread_id)
        healthy = ProjectionResult(
            thread_id=thread_id,
            projected_seq=(
                observed_last_seq
                if replay_window is not None
                else max(current.projected_seq, observed_last_seq)
            ),
            stale=False,
        )
        self._store.record_projection_first_seq(thread_id, observed_first_seq)
        self._store.advance_projection_replay(thread_id, observed_last_seq)
        self._store.update_projection_state(thread_id, healthy, None)
        return healthy

    def _set_stale(
        self,
        thread_id: str,
        projected_seq: int,
        failure_class: str,
        failure_record_id: str | None,
        failure_seq: int,
    ) -> ProjectionResult:
        """保存并返回稳定的 materialization stale 分类。"""
        current, _ = self._store.projection_state(thread_id)
        if current is not None and current.stale:
            return current
        stale = ProjectionResult(
            thread_id=thread_id,
            projected_seq=projected_seq,
            stale=True,
            failure_class=failure_class,
            failure_record_id=failure_record_id,
        )
        self._store.update_projection_state(thread_id, stale, failure_seq)
        return stale
