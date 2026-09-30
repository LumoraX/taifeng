"""审计 Session 里 peer 消息的落账（ADR 0100）。

一条消息有两个事实，由两个写者各写一个：

1. **发出**：发送方写 ``peer_message_sent``，带着消息全文。
2. **进入对话**：目标 thread 的写者写对话项，回指那条发出记录。

第二步不能由发送方代劳。目标 thread 上可能正有 turn 在写，两个写者各自「追加 → 投影」
会打乱投影顺序；更要紧的是对话顺序——消息若落在一次工具调用和它的结果之间，接管时
按 Journal 重建出的 history 就把一对调用拆开了。所以消息先进目标的收件队列，目标的
runner 在迭代边界（调用与结果都已配对的安全点）把它写进对话。

root thread 的收件队列跟着 Session 走，不跟着某个 turn：root 空闲时收到的消息留在
队列里，下一个 root turn 开始时收下。接管时还没进入对话的消息从 Journal 回到队列。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from taifeng.conversation.journal.models import ActorRef
from taifeng.conversation.journal.peer_records import (
    PEER_MESSAGE_SENT_RECORD_TYPE,
    PeerMessageSentV1,
)
from taifeng.conversation.journal.records import (
    ConversationItemV1,
    JournalIdentities,
    JournalRecordFactory,
    conversation_item_record,
    deserialize_response_item,
    record_id,
    serialize_response_item,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.conversation.journal.models import JournalEnvelope
    from taifeng.conversation.models import ResponseItem
    from taifeng.loop.audit_bootstrap import AuditedSessionState

PEER_RECORD_KEY = "peer_record_id"
"""消息对话项 metadata 里的键：它的 ``peer_message_sent`` record id。"""

_ACTOR = ActorRef(kind="system", source="peer")

# coordinator → root thread 的收件队列（跟着 Session 走）
_INBOX: WeakKeyDictionary[object, list[ResponseItem]] = WeakKeyDictionary()


def root_inbox(state: AuditedSessionState) -> list[ResponseItem]:
    """root thread 的收件队列；root runner 把它当作自己的待收输入。"""
    return _INBOX.setdefault(state.coordinator, [])


async def commit_peer_sent(
    state: AuditedSessionState,
    *,
    item: ResponseItem,
    from_thread_id: str,
    mode: str,
    mode_downgraded: bool,
    address: str | None,
) -> ResponseItem:
    """落「发出」；返回带着发出记录 id 的消息，交给目标的收件队列。

    Raises:
        ValueError: 消息内容无法规范化（什么都没有写）。
        SessionAuditFrozenError: Session 已冻结，或写入结果不确定。
    """
    coordinator = state.coordinator
    await coordinator.ensure_effect_allowed()
    sent_id = record_id(item.id, PEER_MESSAGE_SENT_RECORD_TYPE)
    tagged = item.model_copy(update={"metadata": {**item.metadata, PEER_RECORD_KEY: sent_id}})
    factory = JournalRecordFactory(
        session_id=coordinator.session_id,
        actor=_ACTOR,
        identities=JournalIdentities(coordinator.session_id, from_thread_id, item.id),
    )
    record = factory.build(
        operation_id=item.id,
        record_type=PEER_MESSAGE_SENT_RECORD_TYPE,
        payload=PeerMessageSentV1(
            message_id=item.id,
            from_thread_id=from_thread_id,
            to_thread_id=item.thread_id,
            mode=mode,  # type: ignore[arg-type]
            mode_downgraded=mode_downgraded,
            address=address,
            item=serialize_response_item(tagged, source_record_id=sent_id),
        ),
        submission_id=item.id,
        thread_id=from_thread_id,
    )
    await coordinator.append(record)
    return tagged


async def deliver_peer_items(
    state: AuditedSessionState,
    items: Sequence[ResponseItem],
    *,
    submission_id: str,
    turn_index: int,
) -> None:
    """目标 thread 的写者把收下的消息写进对话：一个批次，ack 后推进投影。

    Raises:
        SessionAuditFrozenError: 队列里有没落过「发出」的条目，或写入结果不确定。
    """
    if not items:
        return
    coordinator = state.coordinator
    identities = JournalIdentities(coordinator.session_id, state.thread_id, submission_id)
    factory = JournalRecordFactory(
        session_id=coordinator.session_id, actor=_ACTOR, identities=identities,
    )
    batch = []
    for item in items:
        sent_id = item.metadata.get(PEER_RECORD_KEY)
        if not isinstance(sent_id, str) or item.thread_id != state.thread_id:
            raise coordinator.freeze(
                RuntimeError("pending input without a journaled origin")
            ) from None
        batch.append(conversation_item_record(
            factory,
            operation_id=item.id,
            item=item,
            source_record_id=sent_id,
            ordinal=0,
            submission_id=submission_id,
            turn_id=identities.turn(turn_index),
        ))
    records = tuple(batch)
    ack = await coordinator.append_batch(records)
    envelopes = await coordinator.load_acknowledged(ack, records)
    try:
        projection = await state.projector.project(envelopes, ack)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        raise coordinator.freeze(error) from None
    coordinator.update_projection(projection)


def undelivered_peer_messages(
    envelopes: Sequence[JournalEnvelope], thread_id: str,
) -> tuple[ResponseItem, ...]:
    """发给该 thread、还没有进入对话的消息，按发出顺序。

    Raises:
        pydantic.ValidationError: 消息记录形状违约（Journal 不可信）。
    """
    delivered = {
        ConversationItemV1.model_validate(envelope.payload).source_record_id
        for envelope in envelopes
        if envelope.record_type == "conversation_item" and envelope.thread_id == thread_id
    }
    waiting: list[ResponseItem] = []
    for envelope in envelopes:
        if envelope.record_type != PEER_MESSAGE_SENT_RECORD_TYPE:
            continue
        sent = PeerMessageSentV1.model_validate(envelope.payload)
        if sent.to_thread_id == thread_id and envelope.record_id not in delivered:
            waiting.append(deserialize_response_item(sent.item))
    return tuple(waiting)


__all__ = [
    "PEER_RECORD_KEY",
    "commit_peer_sent",
    "deliver_peer_items",
    "root_inbox",
    "undelivered_peer_messages",
]
