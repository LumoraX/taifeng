"""实验性 SessionJournal durable core（Phase 1）。

本包尚未接入 Engine / MessageStore，也不从 ``taifeng.conversation`` 顶层导出。
"""

from __future__ import annotations

from taifeng.conversation.journal.errors import (
    JournalAlreadyExistsError,
    JournalBusyError,
    JournalConflictError,
    JournalError,
    JournalIntegrityError,
    JournalLeaseError,
    JournalRecoveryRequiredError,
    NonCanonicalValueError,
)
from taifeng.conversation.journal.models import (
    ActorRef,
    Durability,
    JournalAck,
    JournalEnvelope,
    JournalHealth,
    JournalRecord,
    JournalVerification,
    RootThreadDescriptor,
    SessionCreateResult,
    SessionDescriptor,
    SessionLease,
    build_initialization_records,
)

__all__ = [
    "ActorRef",
    "Durability",
    "JournalAck",
    "JournalAlreadyExistsError",
    "JournalBusyError",
    "JournalConflictError",
    "JournalEnvelope",
    "JournalError",
    "JournalHealth",
    "JournalIntegrityError",
    "JournalLeaseError",
    "JournalRecord",
    "JournalRecoveryRequiredError",
    "JournalVerification",
    "NonCanonicalValueError",
    "RootThreadDescriptor",
    "SessionCreateResult",
    "SessionDescriptor",
    "SessionLease",
    "build_initialization_records",
]
