"""持久化层异常类。

错误分类原则：业务异常只通过这两类向外抛，不抛裸 sqlite3 / OSError。
"""

from __future__ import annotations


class DirectoryError(Exception):
    """ThreadDirectory 底层存储错误（IO / 锁争用 / 数据库不可达等）。

    用法：实现侧捕获底层异常 → ``raise DirectoryError(...) from underlying``，
    业务侧通过 ``err.__cause__`` 获取原因。
    """


class ThreadNotFoundError(Exception):
    """``update_metadata`` 引用不存在的 thread_id 时抛出。

    与 ``DirectoryError`` 区分：本异常表示「数据缺失」而非「存储故障」。
    """

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"thread not found: {thread_id}")
        self.thread_id = thread_id
