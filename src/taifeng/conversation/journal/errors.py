"""SessionJournal durable core 的稳定错误分类。"""

from __future__ import annotations


class JournalError(Exception):
    """所有 Journal 领域错误的基类。"""


class NonCanonicalValueError(JournalError):
    """调用方值无法转换为规范 JsonValue。"""

    def __init__(self, reason: str, *, path: str = "$") -> None:
        super().__init__(f"non-canonical value at {path}: {reason}")
        self.reason = reason
        self.path = path


class JournalConflictError(JournalError):
    """record id 内容冲突或 expected seq CAS 冲突。"""

    def __init__(
        self,
        reason: str,
        *,
        expected_seq: int | None = None,
        actual_seq: int | None = None,
        record_id: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.expected_seq = expected_seq
        self.actual_seq = actual_seq
        self.record_id = record_id


class JournalBusyError(JournalError):
    """目标 Session 已由另一个 live writer 持有。"""

    def __init__(self, session_id: str, writer_id: str) -> None:
        super().__init__(f"journal busy: session={session_id}, writer={writer_id}")
        self.session_id = session_id
        self.writer_id = writer_id


class JournalAlreadyExistsError(JournalError):
    """Session 文件已存在，但当前实例没有可复用的 live create result。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"journal already exists: {session_id}")
        self.session_id = session_id


class JournalIntegrityError(JournalError):
    """已提交区域发生不可静默恢复的完整性错误。"""

    def __init__(self, reason: str, *, line_no: int | None = None) -> None:
        suffix = f" at line {line_no}" if line_no is not None else ""
        super().__init__(f"journal integrity error{suffix}: {reason}")
        self.reason = reason
        self.line_no = line_no


class JournalRecoveryRequiredError(JournalError):
    """物理尾未完成，普通执行必须等待显式恢复。"""

    def __init__(self, session_id: str, committed_tail_seq: int) -> None:
        super().__init__(
            f"journal recovery required: session={session_id}, tail={committed_tail_seq}"
        )
        self.session_id = session_id
        self.committed_tail_seq = committed_tail_seq


class JournalLeaseError(JournalError):
    """append 使用了错误、过期或已关闭的 lease。"""

    def __init__(self, session_id: str, reason: str) -> None:
        super().__init__(f"invalid journal lease for {session_id}: {reason}")
        self.session_id = session_id
        self.reason = reason
