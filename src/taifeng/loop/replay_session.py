"""按录制的提交序列重放整个 Session（journal-replay，ADR 0105）。

录制里的用户输入是 ``submission_accepted``（用户消息全文），人的答复是 ``resume_accepted``
（答复原文）。重放器按它们的落账顺序把同样的提交送进一个新的 Engine——LLM 回复来自
``JournalReplayClient``、工具结果来自 ``replay_tools``——每一步等 root turn 停下来（完成、失败、
挂起）再送下一步，最后报告：每一步的结局、有没有分叉、录制里还剩多少没被走到。

重放器不比较对话内容：内核的行为由 LLM 与工具的回复决定，两者都来自录制，新一轮运行走了
录制里没有的路时匹配失败（``ReplayDivergenceError``）就是分叉。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from taifeng.conversation.journal.records import SubmissionAcceptedV1
from taifeng.conversation.journal.suspension_records import (
    RESUME_ACCEPTED_RECORD_TYPE,
    ResumeAcceptedV1,
)
from taifeng.loop.submission import Resume, UserMessage

if TYPE_CHECKING:
    from collections.abc import Iterable

    from taifeng.conversation.journal.models import JournalRecord
    from taifeng.loop.engine import AgentEngine

_TERMINAL_KINDS = frozenset({"turn_completed", "turn_failed", "turn_suspended"})


@dataclass(frozen=True, slots=True)
class RecordedSubmission:
    """录制里的一次提交：用户消息或 ``Resume``。"""

    submission_id: str
    kind: str
    text: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    resolutions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """重放里一步的结局。"""

    recorded_submission_id: str
    kind: str
    submission_id: str
    outcome: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """一次重放的结果。"""

    steps: tuple[ReplayStep, ...]
    diverged_at: str | None
    """分叉发生在哪一步（录制的 submission id）；没有分叉为 None。"""

    @property
    def diverged(self) -> bool:
        """是否分叉。"""
        return self.diverged_at is not None


def recorded_submissions(records: Iterable[JournalRecord]) -> list[RecordedSubmission]:
    """录制里 root thread 上的用户消息与 ``Resume``，按落账顺序。"""
    found: list[RecordedSubmission] = []
    for record in records:
        if record.record_type == "submission_accepted":
            accepted = SubmissionAcceptedV1.model_validate(record.payload)
            if accepted.op_kind != "user_message" or record.submission_id is None:
                continue
            found.append(RecordedSubmission(
                submission_id=record.submission_id,
                kind="user_message",
                text=accepted.text,
                attachments=tuple(
                    dict(a.model_dump(mode="json", exclude={"payload_version"}))
                    for a in accepted.attachments or ()
                ),
            ))
        elif record.record_type == RESUME_ACCEPTED_RECORD_TYPE and record.submission_id:
            resume = ResumeAcceptedV1.model_validate(record.payload)
            found.append(RecordedSubmission(
                submission_id=record.submission_id,
                kind="resume",
                resolutions=dict(resume.resolutions),
            ))
    return found


async def _drive(engine: AgentEngine, op: Any, *, timeout: float) -> tuple[str, str, str | None]:
    """提交一步并等 root turn 停下；返回 (submission id, 结局, 错误)。"""
    events: list[Any] = []
    holder: list[str] = []
    done = asyncio.Event()

    async def collect() -> None:
        async for ev in engine.subscribe_all():
            if not holder or ev.submission_id != holder[0]:
                continue
            events.append(ev)
            kind = ev.msg.kind
            if kind == "turn_suspended" or (kind in _TERMINAL_KINDS and ev.msg.data.get("is_root")):
                done.set()
                return

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    holder.append(await engine.submit(op))
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    last = events[-1]
    error = last.msg.data.get("error") if last.msg.kind == "turn_failed" else None
    if last.msg.kind == "turn_failed":
        return holder[0], str(last.msg.data.get("kind") or "turn_failed"), str(error)
    return holder[0], str(last.msg.kind), None


async def replay_session(
    engine: AgentEngine,
    submissions: Iterable[RecordedSubmission],
    *,
    step_timeout: float = 30.0,
) -> ReplayReport:
    """按录制的提交序列驱动 Engine；分叉即停。

    ``engine`` 应当由回放的 LLM 客户端与回放的工具构成（见 ``JournalReplayClient`` /
    ``replay_tools``）。``Resume`` 的答复原样送入：它们指向的是请求 id，重放里请求 id 由调用 id
    派生、与录制相同。录制里停下等人的调用若在重放里直接拿到了录制的结果（工具被替换成回放版），
    对应的 ``Resume`` 用不上，记为 ``not_needed``。

    Raises:
        TimeoutError: 某一步在 ``step_timeout`` 内没有停下。
    """
    steps: list[ReplayStep] = []
    diverged_at: str | None = None
    for recorded in submissions:
        if recorded.kind == "resume":
            if engine._find_active_suspension() is None:  # noqa: SLF001
                # 录制里等过人的调用在重放里直接拿到了录制的结果，没有停下：这条答复用不上
                steps.append(ReplayStep(recorded.submission_id, "resume", "", "not_needed"))
                continue
            op: Any = Resume(thread_id=engine.thread_id, resolutions=dict(recorded.resolutions))
        else:
            op = UserMessage(text=recorded.text or "", attachments=list(recorded.attachments))
        try:
            submission_id, outcome, error = await _drive(engine, op, timeout=step_timeout)
        except Exception as exc:  # noqa: BLE001  # 准入被拒等：作为这一步的结局报告
            steps.append(ReplayStep(
                recorded.submission_id, recorded.kind, "", type(exc).__name__, str(exc),
            ))
            diverged_at = recorded.submission_id
            break
        steps.append(ReplayStep(recorded.submission_id, recorded.kind, submission_id, outcome, error))
        if outcome in ("ReplayDivergenceError", "ReplayUnsupportedError"):
            diverged_at = recorded.submission_id
            break
    return ReplayReport(tuple(steps), diverged_at)


__all__ = [
    "RecordedSubmission",
    "ReplayReport",
    "ReplayStep",
    "recorded_submissions",
    "replay_session",
]
