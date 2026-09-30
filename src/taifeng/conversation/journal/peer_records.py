"""peer 消息的 durable 记录（session-journal，ADR 0100）。

同一个 Session 里的 agent 之间点对点发消息。发出与进入对话是两件事：

```text
peer_message_sent                       发出（发送方的工具调用结算之前）
conversation_item(user_message)         进入目标 thread 的对话（目标 thread 的写者写下）
```

消息进入对话的时刻由目标 thread 决定：它正在运行的 turn 在下一个迭代边界收下消息；
root thread 空闲时收到的消息等到下一个 root turn 开始。
"""

from __future__ import annotations

from typing import Literal

from taifeng.conversation.journal.models import NonEmptyStr  # noqa: TC001  # Pydantic 运行期需要
from taifeng.conversation.journal.records import ConversationItemV1, PayloadModel

PEER_MESSAGE_SENT_RECORD_TYPE = "peer_message_sent"


class PeerMessageSentV1(PayloadModel):
    """一条 peer 消息被发出。

    Attributes:
        message_id: 消息 id，也是它进入对话后的对话项 id。
        from_thread_id / to_thread_id: 发送方与目标 thread。
        mode: 发送方要求的投递方式。
        mode_downgraded: 要求唤醒、实际只排队（目标正在运行）。
        address: 发送方写的拓扑地址；直接按 thread / 句柄寻址时为空。
        item: 消息本身，即它将以什么样子进入目标 thread 的对话。
    """

    message_id: NonEmptyStr
    from_thread_id: NonEmptyStr
    to_thread_id: NonEmptyStr
    mode: Literal["queue_only", "trigger_turn"]
    mode_downgraded: bool = False
    address: str | None = None
    item: ConversationItemV1


__all__ = ["PEER_MESSAGE_SENT_RECORD_TYPE", "PeerMessageSentV1"]
