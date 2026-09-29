"""分离式派发与 join-barrier 的 durable 记录（session-journal，ADR 0098 / 0099）。

``spawn_skill`` 把子 skill 放到独立的子 thread 上后台运行，发起方不等它结束：

```text
spawn_started + thread_created + thread_bound + conversation_item(种子)   发起（先于子 skill 运行）
…子 thread 上的 LLM / 工具记录…
spawn_settled + thread_terminal                                          结束（done / error / cancelled）
```

join-barrier 等一批派发全部结束，然后在一个新 thread 上起聚合 turn：

```text
barrier_registered                                                        登记
barrier_fired + thread_created + thread_bound + conversation_item(种子)   点火（成员全部结束之后）
…聚合 thread 上的 LLM / 工具记录…
barrier_settled + thread_terminal                                         聚合 turn 结束
```

派发的记录共用一个 operation（句柄 id），barrier 的记录共用一个 operation（barrier id）。
句柄表与 barrier 表由这些记录重建，不依赖对话项。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from taifeng.conversation.journal.models import (  # noqa: TC001  # Pydantic 运行期需要
    HashHex,
    NonEmptyStr,
)
from taifeng.conversation.journal.records import CanonicalMapping, PayloadModel

SPAWN_STARTED_RECORD_TYPE = "spawn_started"
SPAWN_SETTLED_RECORD_TYPE = "spawn_settled"
BARRIER_REGISTERED_RECORD_TYPE = "barrier_registered"
BARRIER_FIRED_RECORD_TYPE = "barrier_fired"
BARRIER_SETTLED_RECORD_TYPE = "barrier_settled"

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


class BarrierRegisteredV1(PayloadModel):
    """登记一个 join-barrier。

    Attributes:
        barrier_id: barrier id。
        handle_ids: 要等的派发，按登记顺序。
        then_skill_id: 成员全部结束后起的聚合 skill。
        then_args_template: 聚合 skill 的自定义输入；没有则用各成员的终态。
    """

    barrier_id: NonEmptyStr
    handle_ids: tuple[NonEmptyStr, ...]
    then_skill_id: NonEmptyStr
    then_args_template: CanonicalMapping | None = None

    @model_validator(mode="after")
    def _require_distinct_members(self) -> BarrierRegisteredV1:
        """成员不得重复。"""
        if len(set(self.handle_ids)) != len(self.handle_ids):
            raise ValueError("barrier members must be distinct")
        return self


class BarrierMemberV1(PayloadModel):
    """点火时一个成员的终态。"""

    handle_id: NonEmptyStr
    status: SpawnStatusV1


class BarrierFiredV1(PayloadModel):
    """barrier 点火：成员全部结束，聚合 turn 即将运行。

    Attributes:
        barrier_id: barrier id。
        registered_record_id: 对应的 ``barrier_registered`` record。
        then_skill_id / then_thread_id: 聚合 skill 与它运行所在的 thread。
        members: 点火时各成员的终态，顺序与登记时一致。
        arguments: 交给聚合 skill 的输入。
        definition_hash / body_hash: 点火时聚合 skill 定义与正文的摘要。
    """

    barrier_id: NonEmptyStr
    registered_record_id: NonEmptyStr
    then_skill_id: NonEmptyStr
    then_thread_id: NonEmptyStr
    members: tuple[BarrierMemberV1, ...]
    arguments: CanonicalMapping
    definition_hash: HashHex
    body_hash: HashHex


class BarrierSettledV1(PayloadModel):
    """聚合 turn 结束。

    Attributes:
        barrier_id: barrier id。
        then_thread_id: 聚合 thread。
        fired_record_id: 对应的 ``barrier_fired`` record。
        status: 终态。
        end_reason: 聚合 turn 的结束方式；接管时补写的为 ``process_recovery``。
        result: ``done`` 时是聚合 skill 的最终文本，``error`` 时是错误说明。
    """

    barrier_id: NonEmptyStr
    then_thread_id: NonEmptyStr
    fired_record_id: NonEmptyStr
    status: SpawnStatusV1
    end_reason: NonEmptyStr
    result: str | None = None


__all__ = [
    "BARRIER_FIRED_RECORD_TYPE",
    "BARRIER_REGISTERED_RECORD_TYPE",
    "BARRIER_SETTLED_RECORD_TYPE",
    "SPAWN_RECOVERY_END_REASON",
    "SPAWN_SETTLED_RECORD_TYPE",
    "SPAWN_STARTED_RECORD_TYPE",
    "BarrierFiredV1",
    "BarrierMemberV1",
    "BarrierRegisteredV1",
    "BarrierSettledV1",
    "SpawnSettledV1",
    "SpawnStartedV1",
    "SpawnStatusV1",
]
