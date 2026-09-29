"""分离式派发的 durable 记录（session-journal，ADR 0098）。

``spawn_skill`` 把子 skill 放到独立的子 thread 上后台运行，发起方不等它结束：

```text
spawn_started + thread_created + thread_bound + conversation_item(种子)   发起（先于子 skill 运行）
…子 thread 上的 LLM / 工具记录…
spawn_settled + thread_terminal                                          结束（done / error / cancelled）
```

两条记录共用一个 operation（句柄 id）。句柄表由这些记录重建，不依赖对话项。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from taifeng.conversation.journal.models import (  # noqa: TC001  # Pydantic 运行期需要
    HashHex,
    NonEmptyStr,
)
from taifeng.conversation.journal.records import CanonicalMapping, PayloadModel

SPAWN_STARTED_RECORD_TYPE = "spawn_started"
SPAWN_SETTLED_RECORD_TYPE = "spawn_settled"

SPAWN_RECOVERY_END_REASON = "process_recovery"
"""接管时发现仍在运行的派发的结束原因：进程已不在，子 skill 没有跑完。"""

SpawnStatusV1 = Literal["done", "error", "cancelled"]
"""分离式派发的终态。"""


class SpawnStartedV1(PayloadModel):
    """一次分离式派发被发起。

    Attributes:
        handle_id: 句柄 id；之后的查询、等待、终止都以它为键。
        skill_id: 被派发的 skill。
        child_thread_id: 子 skill 运行所在的 thread。
        parent_thread_id: 发起方所在 Session 的 root thread。
        reason: 发起方自陈的理由。
        arguments: 交给子 skill 的种子输入。
        definition_hash / body_hash: 派发时该 skill 定义与正文的摘要。
        deadline_seconds: 墙钟上限；没有为 None。
    """

    handle_id: NonEmptyStr
    skill_id: NonEmptyStr
    child_thread_id: NonEmptyStr
    parent_thread_id: NonEmptyStr
    reason: str
    arguments: CanonicalMapping
    definition_hash: HashHex
    body_hash: HashHex
    deadline_seconds: float | None = Field(default=None, gt=0)


class SpawnSettledV1(PayloadModel):
    """一次分离式派发结束。

    Attributes:
        handle_id: 句柄 id。
        child_thread_id: 子 thread。
        started_record_id: 对应的 ``spawn_started`` record。
        status: 终态。
        end_reason: 子 turn 的结束方式；接管时补写的为 ``process_recovery``。
        result: ``done`` 时是子 skill 的最终文本，``error`` 时是错误说明，``cancelled`` 时为空。
    """

    handle_id: NonEmptyStr
    child_thread_id: NonEmptyStr
    started_record_id: NonEmptyStr
    status: SpawnStatusV1
    end_reason: NonEmptyStr
    result: str | None = None


__all__ = [
    "SPAWN_RECOVERY_END_REASON",
    "SPAWN_SETTLED_RECORD_TYPE",
    "SPAWN_STARTED_RECORD_TYPE",
    "SpawnSettledV1",
    "SpawnStartedV1",
    "SpawnStatusV1",
]
