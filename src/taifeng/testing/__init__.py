"""taifeng.testing —— 给适配包用的一致性检查（不依赖任何测试框架）。

内核把可替换的部分定成协议；外部实现是否真的满足协议的**行为**约定，靠这里的检查验收：

- ``journal_conformance``：SessionJournal core（``SessionJournalCore``）的行为；
- ``journal_adapter_conformance``：注入 ``JsonlSessionJournalCore`` 的 ``SyncFileAdapter`` /
  ``WriterLockAdapter``。

每个检查是一个具名的异步函数，失败抛 ``ConformanceFailure``（``AssertionError`` 的子类）。用 pytest 的
适配包按名字参数化即可；不用 pytest 的直接 ``await`` 它们。
"""

from __future__ import annotations

from taifeng.testing.journal_adapter_conformance import (
    JournalStorageHarness,
    journal_storage_cases,
    run_cases,
    writer_lock_cases,
)
from taifeng.testing.journal_conformance import (
    ConformanceCase,
    ConformanceFailure,
    JournalCoreHarness,
    journal_core_cases,
)

__all__ = [
    "ConformanceCase",
    "ConformanceFailure",
    "JournalCoreHarness",
    "JournalStorageHarness",
    "journal_core_cases",
    "journal_storage_cases",
    "run_cases",
    "writer_lock_cases",
]
