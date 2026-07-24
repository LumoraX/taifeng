"""单 Session 的 Journal 追加、冻结、取消与投影状态协调。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from taifeng.conversation.journal.projector import ProjectionResult
from taifeng.conversation.journal.records import StableErrorV1
from taifeng.loop.audit_journal import (
    JournalAppendCore,
    JournalAppendReceipt,
    validate_ack,
    validate_acknowledged_envelopes,
    validate_records,
)
from taifeng.loop.audit_lifecycle import (
    AcceptedWork,
    SessionFinishingError,
    SessionFinishResult,
    SessionLifecycle,
    ThreadTerminalRequest,
    _AdmissionReservation,
    _build_terminal_records,
    _FinishFuture,
)
from taifeng.loop.audit_support import (
    AuditHealth,
    ProjectionAuditSnapshot,
    SessionAuditFrozenError,
    SessionAuditSnapshot,
    _accepted_work_ownership_failure,
    _await_owned,
    _copy_projection_snapshot,
    _copy_stable_error,
    _InvalidJournalAckError,
    _journal_failure,
    _projection_failure,
)
from taifeng.loop.cancellation import CancellationToken

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from taifeng.conversation.journal.models import (
        JournalAck,
        JournalEnvelope,
        JournalRecord,
        SessionLease,
    )


class SessionAuditCoordinator:
    """串行化一个 Session 的 Journal append，并隔离其 fail-closed 状态。"""

    def __init__(
        self,
        *,
        core: JournalAppendCore,
        lease: SessionLease,
        expected_seq: int,
        session_root_cancel: CancellationToken | None = None,
        finish_timeout: float = 30.0,
    ) -> None:
        """注入 core/lease、初始化尾序号、root token 与有界终结超时。"""
        if expected_seq < 0:
            raise ValueError("expected_seq must be non-negative")
        if finish_timeout <= 0:
            raise ValueError("finish_timeout must be positive")
        self._core = core
        self._lease = lease
        self._expected_seq = expected_seq
        self._finish_timeout = finish_timeout
        self._session_root_cancel = session_root_cancel or CancellationToken(
            name=f"session:{lease.session_id}"
        )
        self._append_lock = anyio.Lock()
        self._lifecycle_lock = anyio.Lock()
        self._health = AuditHealth.HEALTHY
        self._effect_gate_open = True
        self._frozen_error: SessionAuditFrozenError | None = None
        self._first_failure: StableErrorV1 | None = None
        self._targets: dict[str, CancellationToken] = {}
        self._projections: dict[str, ProjectionAuditSnapshot] = {}
        self._lifecycle = SessionLifecycle.OPEN
        self._audit_complete: bool | None = None
        self._lease_released: bool | None = None
        self._admissions: dict[str, _AdmissionReservation] = {}
        self._finish_future: _FinishFuture | None = None
        self._committed_terminal_threads: set[str] = set()
        self._terminal_sealed = False

    @property
    def session_id(self) -> str:
        """返回 coordinator 绑定的 Session id。"""
        return self._lease.session_id

    @property
    def expected_seq(self) -> int:
        """返回下一批 CAS 使用的 committed tail seq。"""
        return self._expected_seq

    @property
    def health(self) -> AuditHealth:
        """返回当前审计健康状态。"""
        return self._health

    @property
    def effect_gate_open(self) -> bool:
        """返回新 effect 是否仍可进入。"""
        return self._effect_gate_open

    @property
    def session_root_cancel(self) -> CancellationToken:
        """返回 Session root token，供 Engine/Turn 派生取消 subtree。"""
        return self._session_root_cancel

    async def append(
        self,
        record: JournalRecord,
        *,
        cancel: CancellationToken | None = None,
    ) -> JournalAck:
        """串行追加单条 runtime-owned record。"""
        return await self.append_batch((record,), cancel=cancel)

    async def append_batch(
        self,
        records: Sequence[JournalRecord],
        *,
        cancel: CancellationToken | None = None,
    ) -> JournalAck:
        """兼容入口：串行追加 batch 并返回原有 JournalAck。"""
        receipt = await self.append_batch_receipt(records, cancel=cancel)
        return receipt.ack

    async def append_batch_receipt(
        self,
        records: Sequence[JournalRecord],
        *,
        cancel: CancellationToken | None = None,
    ) -> JournalAppendReceipt:
        """串行追加并显式返回新提交或 historical ack 的 race-free 裁定。"""
        self._raise_if_frozen()
        self._raise_if_append_sealed()
        snapshot = validate_records(records, session_id=self.session_id)
        if cancel is not None:
            cancel.raise_if_cancelled()
        async with self._append_lock:
            self._raise_if_frozen()
            self._raise_if_append_sealed()
            if cancel is not None:
                cancel.raise_if_cancelled()
            return await self._append_locked(snapshot)

    async def load_acknowledged(
        self,
        ack: JournalAck,
        expected_records: Sequence[JournalRecord],
    ) -> tuple[JournalEnvelope, ...]:
        """strict 读取 receipt，并与刚提交的 records/covering ack 做 exact 比对。"""
        records = validate_records(expected_records, session_id=self.session_id)
        try:
            loaded = tuple([
                envelope
                async for envelope in self._core.load(
                    self.session_id,
                    after_seq=ack.first_seq - 1,
                )
                if envelope.seq <= ack.last_seq
            ])
        except (KeyboardInterrupt, SystemExit) as error:
            self.freeze(error)
            raise
        except BaseException as error:
            if isinstance(error, anyio.get_cancelled_exc_class()):
                self.freeze(error)
                raise
            raise self.freeze(error) from None
        try:
            return validate_acknowledged_envelopes(
                loaded,
                ack=ack,
                records=records,
            )
        except _InvalidJournalAckError:
            raise self.freeze(_InvalidJournalAckError()) from None

    def _raise_if_append_sealed(self) -> None:
        """terminal seal 或 CLOSED 后在 core dispatch 前稳定拒绝普通 append。"""
        if self._terminal_sealed or self._lifecycle is SessionLifecycle.CLOSED:
            raise SessionFinishingError(self.session_id, self._lifecycle)

    async def _append_locked(
        self,
        records: tuple[JournalRecord, ...],
    ) -> JournalAppendReceipt:
        """在 append lock 内调用 core，并把任何 runtime 边界失败原子冻结。"""
        expected_seq = self._expected_seq
        failure: BaseException | None = None
        try:
            ack = await self._core.append_batch(
                records,
                lease=self._lease,
                expected_seq=expected_seq,
            )
            ack = validate_ack(
                ack,
                records,
                session_id=self.session_id,
                writer_epoch=self._lease.writer_epoch,
                expected_seq=self._expected_seq,
            )
        except (KeyboardInterrupt, SystemExit) as error:
            self.freeze(error)
            raise
        except BaseException as error:
            if isinstance(error, anyio.get_cancelled_exc_class()):
                raise
            failure = error
        if failure is not None:
            self.freeze(failure)
            failure = None
            self._raise_if_frozen()
        self._expected_seq = max(self._expected_seq, ack.last_seq)
        self._remember_terminal_threads(records)
        return JournalAppendReceipt(
            ack=ack,
            newly_committed=ack.first_seq == expected_seq + 1,
        )

    def _remember_terminal_threads(self, records: tuple[JournalRecord, ...]) -> None:
        """只在 durable ack 后登记已提交的 thread terminal，供 finish 去重。"""
        for record in records:
            if record.record_type == "thread_terminal" and record.thread_id is not None:
                self._committed_terminal_threads.add(record.thread_id)
    async def admit_work(
        self,
        work_id: str,
        durable_accept: Callable[[], Awaitable[None]],
    ) -> AcceptedWork:
        """shared lock 内登记 reservation，锁外 accept，再回锁结算。"""
        reservation = await self._reserve_admission(work_id)
        settlement_name = f"audit-admission-settle:{self.session_id}:{work_id}"
        retirement_name = f"audit-admission-retire:{self.session_id}:{work_id}"
        try:
            await durable_accept()
        except BaseException:
            await _await_owned(self._settle_admission(
                reservation, accepted=False,
            ), name=settlement_name)
            raise
        settlement, cancellation = await _await_owned(
            self._settle_admission(reservation, accepted=True),
            name=settlement_name,
        )
        work, lifecycle = settlement
        assert work is not None
        if cancellation is not None:
            self.freeze(_accepted_work_ownership_failure())
            await _await_owned(work.complete(), name=retirement_name)
            raise cancellation
        try:
            self._raise_if_frozen()
            if lifecycle is SessionLifecycle.CLOSED:
                raise SessionFinishingError(self.session_id, lifecycle)
        except (SessionAuditFrozenError, SessionFinishingError):
            await _await_owned(work.complete(), name=retirement_name)
            raise
        return work

    async def reject_work(
        self,
        work_id: str,
        durable_reject: Callable[[], Awaitable[None]],
    ) -> None:
        """在 OPEN 内登记 rejection，使 finish 等待其 durable 结果后再封口。"""
        reservation = await self._reserve_admission(work_id)
        try:
            await durable_reject()
        finally:
            _, cancellation = await _await_owned(
                self._settle_admission(reservation, accepted=False),
                name=f"audit-admission-settle:{self.session_id}:{work_id}",
            )
        if cancellation is not None:
            raise cancellation

    async def _reserve_admission(self, work_id: str) -> _AdmissionReservation:
        """在 shared lifecycle lock 内占用唯一 submission identity。"""
        if not work_id:
            raise ValueError("work_id must be non-empty")
        async with self._lifecycle_lock:
            if self._lifecycle is not SessionLifecycle.OPEN:
                raise SessionFinishingError(self.session_id, self._lifecycle)
            self._raise_if_frozen()
            self._prune_completed_admissions()
            if work_id in self._admissions:
                raise ValueError(f"work already accepted: {work_id}")
            reservation = _AdmissionReservation(work_id)
            self._admissions[work_id] = reservation
        return reservation

    def ensure_intake_open(self) -> None:
        """在首个 await 前让 FINISHING/CLOSED 优先于输入 canonical 校验。"""
        if self._lifecycle is not SessionLifecycle.OPEN:
            raise SessionFinishingError(self.session_id, self._lifecycle)
        self._raise_if_frozen()

    async def close_intake(self) -> SessionLifecycle:
        """与 admission 共锁地关闭新 intake，已登记 reservation 继续收敛。"""
        lifecycle: SessionLifecycle | None = None
        with anyio.CancelScope(shield=True):
            async with self._lifecycle_lock:
                if self._lifecycle is SessionLifecycle.OPEN:
                    self._lifecycle = SessionLifecycle.FINISHING
                lifecycle = self._lifecycle
        assert lifecycle is not None
        return lifecycle

    async def _settle_admission(
        self,
        reservation: _AdmissionReservation,
        *,
        accepted: bool,
    ) -> tuple[AcceptedWork | None, SessionLifecycle]:
        """在 cancellation-independent shared lock 内结算 reservation。"""
        result: tuple[AcceptedWork | None, SessionLifecycle] | None = None
        with anyio.CancelScope(shield=True):
            async with self._lifecycle_lock:
                if not accepted:
                    reservation.settle_failed()
                    if self._admissions.get(reservation.work_id) is reservation:
                        self._admissions.pop(reservation.work_id)
                    result = (None, self._lifecycle)
                else:
                    work = AcceptedWork(
                        work_id=reservation.work_id,
                        _completed=anyio.Event(),
                        _retire=lambda accepted: self._retire_accepted_work(
                            reservation,
                            accepted,
                        ),
                    )
                    reservation.settle_accepted(work)
                    result = (work, self._lifecycle)
        assert result is not None
        return result

    async def _retire_accepted_work(
        self,
        reservation: _AdmissionReservation,
        work: AcceptedWork,
    ) -> None:
        """按 reservation/work identity 即时退休，并唤醒已快照的 finish waiter。"""
        with anyio.CancelScope(shield=True):
            async with self._lifecycle_lock:
                if reservation.accepted_work is not work:
                    return
                if self._admissions.get(reservation.work_id) is reservation:
                    self._admissions.pop(reservation.work_id)
                work._mark_completed()  # noqa: SLF001

    async def finish(
        self,
        *,
        thread_terminals: Sequence[ThreadTerminalRequest],
        reason: str,
        status: str = "complete",
    ) -> SessionFinishResult:
        """唯一胜者关闭 intake/快照 work；所有 caller 等待同一 bounded future。"""
        result: SessionFinishResult | None = None
        with anyio.CancelScope(shield=True):
            async with self._lifecycle_lock:
                future = self._finish_future
                owner = future is None
                if owner:
                    requests = self._validated_terminal_requests(thread_terminals)
                    if not reason or not status:
                        raise ValueError("finish status and reason must be non-empty")
                    future = _FinishFuture()
                    self._finish_future = future
                    self._lifecycle = SessionLifecycle.FINISHING
                    self._prune_completed_admissions()
                    reservations = tuple(self._admissions.values())
                else:
                    requests, reservations = (), ()
            assert future is not None
            if owner:
                await self._drive_finish(future, reservations, requests, status, reason)
            result = await future.wait()
        assert result is not None
        return result

    def _prune_completed_admissions(self) -> None:
        """在 lifecycle lock 内只保留 pending 或 accepted-incomplete reservation。"""
        self._admissions = {
            work_id: reservation
            for work_id, reservation in self._admissions.items()
            if reservation.is_incomplete
        }

    @staticmethod
    def _validated_terminal_requests(
        requests: Sequence[ThreadTerminalRequest],
    ) -> tuple[ThreadTerminalRequest, ...]:
        """快照、重建并拒绝非 DTO 或重复 thread id。"""
        snapshot = tuple(requests)
        if any(not isinstance(request, ThreadTerminalRequest) for request in snapshot):
            raise TypeError("finish accepts only ThreadTerminalRequest values")
        copied = tuple(
            ThreadTerminalRequest(
                thread_id=request.thread_id,
                status=request.status,
                end_reason=request.end_reason,
                stable_error=(
                    _copy_stable_error(request.stable_error)
                    if request.stable_error is not None
                    else None
                ),
            )
            for request in snapshot
        )
        thread_ids = tuple(request.thread_id for request in copied)
        if len(set(thread_ids)) != len(thread_ids):
            raise ValueError("finish thread ids must be unique")
        return copied

    async def _drive_finish(
        self,
        future: _FinishFuture,
        reservations: tuple[_AdmissionReservation, ...],
        requests: tuple[ThreadTerminalRequest, ...],
        status: str,
        reason: str,
    ) -> None:
        """在 shield 内有界收敛 work、terminal batch 与唯一 close。"""
        terminal_record_ids: tuple[str, ...] = ()
        terminal_acked = False
        close_attempted = False
        lease_released = False
        failure: StableErrorV1 | None = None
        fatal_to_raise: KeyboardInterrupt | SystemExit | None = None
        try:
            with anyio.fail_after(self._finish_timeout):
                await self._await_accepted_work(reservations)
                ack = await self._append_terminal_batch(
                    requests=requests,
                    status=status,
                    reason=reason,
                )
                terminal_record_ids = ack.record_ids
                terminal_acked = True
                self._raise_if_frozen()
                close_attempted = True
                await self._core.close_session(self._lease)
                lease_released = True
                self._raise_if_frozen()
        except BaseException as error:
            failure, fatal_to_raise, emergency_released = (
                await self._settle_finish_failure(
                    error=error,
                    reservations=reservations,
                    close_attempted=close_attempted,
                )
            )
            if not close_attempted:
                lease_released = emergency_released
        await self._publish_finish_result(
            future=future,
            failure=failure,
            audit_complete=terminal_acked,
            lease_released=lease_released,
            terminal_record_ids=terminal_record_ids,
        )
        if fatal_to_raise is not None:
            raise fatal_to_raise

    async def _await_accepted_work(
        self,
        reservations: tuple[_AdmissionReservation, ...],
    ) -> None:
        """等待 finish 快照内 reservation 结算及 accepted work 收敛。"""
        for reservation in reservations:
            await reservation.wait_settled()
        accepted = tuple(
            work
            for reservation in reservations
            if (work := reservation.accepted_work) is not None
        )
        for work in accepted:
            await work.wait_completed()

    async def _append_terminal_batch(
        self,
        *,
        requests: tuple[ThreadTerminalRequest, ...],
        status: str,
        reason: str,
    ) -> JournalAck:
        """在 append lock 内读取最新去重集合、封口并提交 terminal batch。"""
        async with self._append_lock:
            self._raise_if_frozen()
            if self._terminal_sealed or self._lifecycle is SessionLifecycle.CLOSED:
                raise SessionFinishingError(self.session_id, self._lifecycle)
            self._terminal_sealed = True
            records = _build_terminal_records(
                session_id=self.session_id,
                requests=requests,
                committed_thread_ids=self._committed_terminal_threads,
                status=status,
                reason=reason,
            )
            receipt = await self._append_locked(records)
            return receipt.ack

    async def _settle_finish_failure(
        self,
        *,
        error: BaseException,
        reservations: tuple[_AdmissionReservation, ...],
        close_attempted: bool,
    ) -> tuple[StableErrorV1, KeyboardInterrupt | SystemExit | None, bool]:
        """稳定化 finish 首因、尝试 emergency close，并选定待重抛 fatal。"""
        fatal = error if isinstance(error, (KeyboardInterrupt, SystemExit)) else None
        incomplete = tuple(
            reservation for reservation in reservations if reservation.is_incomplete
        )
        if isinstance(error, TimeoutError) and incomplete:
            failure = StableErrorV1(
                code="accepted_work_convergence_timeout",
                class_name="AcceptedWorkConvergenceTimeout",
                failure_class="lifecycle",
                retryable=False,
            )
        else:
            failure = self._finish_failure(error)
        self.freeze(failure)
        if close_attempted:
            return failure, fatal, False
        try:
            lease_released = await self._emergency_close()
        except (KeyboardInterrupt, SystemExit) as emergency_fatal:
            fatal = fatal or emergency_fatal
            lease_released = False
        return failure, fatal, lease_released

    async def _publish_finish_result(
        self,
        *,
        future: _FinishFuture,
        failure: StableErrorV1 | None,
        audit_complete: bool,
        lease_released: bool,
        terminal_record_ids: tuple[str, ...],
    ) -> None:
        """在 lifecycle lock 内一次性发布共享结果并关闭 intake/effect gate。"""
        async with self._lifecycle_lock:
            if failure is None and self._frozen_error is not None:
                failure = self._finish_failure(self._frozen_error)
            result = SessionFinishResult(
                session_id=self.session_id,
                audit_complete=audit_complete,
                lease_released=lease_released,
                terminal_record_ids=terminal_record_ids,
                _failure=_copy_stable_error(failure) if failure is not None else None,
            )
            self._audit_complete = result.audit_complete
            self._lease_released = result.lease_released
            self._effect_gate_open = False
            self._lifecycle = SessionLifecycle.CLOSED
            if result.audit_complete:
                self._admissions.clear()
            else:
                self._admissions = {
                    work_id: reservation
                    for work_id, reservation in self._admissions.items()
                    if reservation.is_incomplete
                }
            future.set_result(result)

    def _finish_failure(self, error: BaseException) -> StableErrorV1:
        """保留 Journal 首因；其他 timeout/close 异常只映射稳定字段。"""
        if self._first_failure is not None:
            return _copy_stable_error(self._first_failure)
        if isinstance(error, SessionAuditFrozenError):
            return error.cause
        return _journal_failure(error)

    async def _emergency_close(self) -> bool:
        """终结不完整时 bounded close；返回确定释放结论，fatal 原样传播。"""
        with anyio.move_on_after(self._finish_timeout, shield=True) as scope:
            try:
                await self._core.close_session(self._lease)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                return False
        return not scope.cancel_called

    async def ensure_effect_allowed(self) -> None:
        """effect 前 fail-closed 检查；冻结后永远返回同一稳定错误。"""
        self._raise_if_frozen()
        if self._lifecycle is SessionLifecycle.CLOSED:
            raise SessionFinishingError(self.session_id, self._lifecycle)

    def freeze(
        self,
        cause: BaseException | StableErrorV1,
    ) -> SessionAuditFrozenError:
        """第一次调用同步、无 await 地固定首因、关 gate 并取消整个 Session。"""
        if self._frozen_error is not None:
            return self._frozen_error
        stable_cause = (
            _copy_stable_error(cause)
            if isinstance(cause, StableErrorV1)
            else _journal_failure(cause)
        )
        self._first_failure = _copy_stable_error(stable_cause)
        frozen = SessionAuditFrozenError(self.session_id, stable_cause)
        self._frozen_error = frozen
        self._health = AuditHealth.RECOVERY_REQUIRED
        self._effect_gate_open = False
        self._session_root_cancel.cancel()
        for target in tuple(self._targets.values()):
            target.cancel()
        return frozen

    def _raise_if_frozen(self) -> None:
        """重抛唯一冻结错误，并清理旧 traceback/context 引用。"""
        frozen = self._frozen_error
        if frozen is None:
            return
        frozen.__traceback__ = None
        frozen.__cause__ = None
        frozen.__context__ = None
        frozen.__suppress_context__ = True
        raise frozen from None

    def register_target(self, target_id: str) -> CancellationToken:
        """登记 active turn token；其 child 自动形成目标取消 subtree。"""
        self._raise_if_frozen()
        if not target_id:
            raise ValueError("target_id must be non-empty")
        if target_id in self._targets:
            raise ValueError(f"target already registered: {target_id}")
        target = self._session_root_cancel.child(f"target:{target_id}")
        self._targets[target_id] = target
        return target

    def unregister_target(
        self,
        target_id: str,
        token: CancellationToken,
    ) -> bool:
        """仅当 id/token 仍对应同一 active turn 时注销，避免旧任务误删新任务。"""
        current = self._targets.get(target_id)
        if current is not token:
            return False
        self._targets.pop(target_id)
        token._detach_from_parent()  # noqa: SLF001
        return True

    def cancel_target(self, target_id: str) -> bool:
        """只取消目标 turn 及其 child subtree，不影响 Session root/peer target。"""
        target = self._targets.get(target_id)
        if target is None:
            return False
        target.cancel()
        return True

    def mark_projection_stale(
        self,
        result: ProjectionResult,
    ) -> ProjectionAuditSnapshot:
        """只镜像可信 stale 结果；不接收 raw exception 或猜测水位。"""
        validated = self._validated_projection_result(result)
        if not validated.stale:
            raise ValueError("mark_projection_stale requires a stale result")
        return self._mark_projection_stale(validated)

    def _mark_projection_stale(
        self,
        result: ProjectionResult,
    ) -> ProjectionAuditSnapshot:
        """把已重验 stale 结果单调写入目标 thread 状态。"""
        current = self._projections.get(result.thread_id)
        incoming_seq = result.projected_seq
        if current is not None and not current.stale and incoming_seq < current.projected_seq:
            return _copy_projection_snapshot(current)
        projected_seq = max(
            current.projected_seq if current is not None else 0,
            incoming_seq,
        )
        state = ProjectionAuditSnapshot(
            thread_id=result.thread_id,
            projected_seq=projected_seq,
            stale=True,
            failure=current.failure
            if current is not None and current.stale
            else _projection_failure(result),
        )
        return self._store_projection(state)

    def update_projection(self, result: ProjectionResult) -> ProjectionAuditSnapshot:
        """重验并镜像 projector 结果；健康 replay 单调推进或清 stale。"""
        validated = self._validated_projection_result(result)
        if validated.stale:
            return self._mark_projection_stale(validated)
        current = self._projections.get(validated.thread_id)
        if current is None:
            state = ProjectionAuditSnapshot(
                thread_id=validated.thread_id,
                projected_seq=validated.projected_seq,
                stale=False,
            )
        else:
            state = self._healthy_projection_state(current, validated.projected_seq)
        return self._store_projection(state)

    def _store_projection(
        self,
        state: ProjectionAuditSnapshot,
    ) -> ProjectionAuditSnapshot:
        """内部保存与 caller 返回各自深复制，永不共享 snapshot/failure。"""
        internal = _copy_projection_snapshot(state)
        self._projections[state.thread_id] = internal
        return _copy_projection_snapshot(internal)

    @staticmethod
    def _validated_projection_result(result: ProjectionResult) -> ProjectionResult:
        """拒绝错误类型，并重建 frozen DTO 以防 object.__setattr__ 绕过构造校验。"""
        if not isinstance(result, ProjectionResult):
            raise TypeError("coordinator accepts only ProjectionResult")
        return ProjectionResult(
            thread_id=result.thread_id,
            projected_seq=result.projected_seq,
            stale=result.stale,
            failure_class=result.failure_class,
            failure_record_id=result.failure_record_id,
        )

    @staticmethod
    def _healthy_projection_state(
        current: ProjectionAuditSnapshot,
        replayed_seq: int,
    ) -> ProjectionAuditSnapshot:
        """可信 healthy 结果不低于当前水位才清 stale，旧 replay 完全忽略。"""
        if replayed_seq < current.projected_seq:
            return current
        return ProjectionAuditSnapshot(
            thread_id=current.thread_id,
            projected_seq=replayed_seq,
            stale=False,
        )

    def projection_snapshot(self, thread_id: str) -> ProjectionAuditSnapshot | None:
        """读取一个 thread 的冻结投影快照。"""
        state = self._projections.get(thread_id)
        return _copy_projection_snapshot(state) if state is not None else None

    def snapshot(self) -> SessionAuditSnapshot:
        """返回不暴露可变 mapping/token/core 的只读状态快照。"""
        return SessionAuditSnapshot(
            session_id=self.session_id,
            expected_seq=self._expected_seq,
            health=self._health,
            effect_gate_open=self._effect_gate_open,
            root_cancelled=self._session_root_cancel.is_cancelled,
            first_failure=(
                _copy_stable_error(self._first_failure)
                if self._first_failure is not None
                else None
            ),
            active_target_ids=tuple(sorted(self._targets)),
            projections=tuple(
                _copy_projection_snapshot(self._projections[thread_id])
                for thread_id in sorted(self._projections)
            ),
            lifecycle=self._lifecycle,
            audit_complete=self._audit_complete,
            lease_released=self._lease_released,
            accepted_work_ids=tuple(sorted(self._admissions)),
        )


__all__ = [
    "AcceptedWork",
    "AuditHealth",
    "JournalAppendReceipt",
    "ProjectionAuditSnapshot",
    "SessionAuditCoordinator",
    "SessionAuditFrozenError",
    "SessionAuditSnapshot",
    "SessionFinishResult",
    "SessionFinishingError",
    "SessionLifecycle",
    "ThreadTerminalRequest",
]
