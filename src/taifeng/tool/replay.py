"""用录制的 SessionJournal 确定性回放工具结果（journal-replay，ADR 0105）。

``JournalReplayClient`` 让 LLM 的回复来自录制；本模块让工具的结果也来自录制：一次工具调用按
（工具名，规范化后的有效参数）匹配到录制里同名同参的那次调用，取它的结果交回内核，工具本身不
执行。两者合起来，一个录制过的 Session 可以在不触网、不碰外部系统的情况下整条重放。

匹配规则：

- 按内容而不是按顺序匹配：并发的调用顺序不稳定，内容能正确配对；
- 同名同参的多次录制按录制顺序依次消费；
- 找不到匹配 → ``ReplayDivergenceError``——新一轮运行走了录制里没有的路，这正是回归信号。

还原：录制的 ``tool_outcome_committed`` 给出文本与终态；同批的 ``function_call_output`` 对话项给出
图片附件。``rejected`` / ``cancelled`` 的录制照样回放成错误结果（那是当时的事实）；``unknown``
的录制不能回放（``ReplayUnsupportedError``）。

参照：claw-code ``prompt_cache.rs`` 按请求哈希存取响应；差异：数据源是审计 Journal，不另建录制格式。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from taifeng.conversation.journal.canonical import canonical_hash
from taifeng.conversation.journal.records import (
    ConversationItemV1,
    ToolIntentCommittedV1,
    ToolOutcomeCommittedV1,
)
from taifeng.llm.image_input import ImageAttachmentV1
from taifeng.llm.providers.replay import ReplayDivergenceError, ReplayUnsupportedError
from taifeng.tool.spec import ToolResult, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Iterable

    from taifeng.conversation.journal.models import JournalRecord
    from taifeng.tool.spec import ToolContext


@dataclass(frozen=True, slots=True)
class RecordedToolCall:
    """一次录制的工具调用：意图（名字、参数）与结果。"""

    intent_record_id: str
    call_id: str
    name: str
    arguments_hash: str
    status: str
    output: str
    attachments: tuple[ImageAttachmentV1, ...] = ()
    error_code: str | None = None

    def result(self) -> ToolResult:
        """还原成工具结果。

        Raises:
            ReplayUnsupportedError: 录制的终态是 ``unknown``，当时就没有结果。
        """
        if self.status == "unknown":
            raise ReplayUnsupportedError(
                f"recorded tool call {self.call_id} has no usable outcome (unknown)"
            )
        if self.status == "success":
            return ToolResult.ok(self.output, attachments=self.attachments)
        return ToolResult.error(self.output, reason=self.error_code or self.status)


def arguments_hash(arguments: dict[str, Any]) -> str:
    """有效参数的规范化摘要（匹配键）。"""
    return canonical_hash(arguments)


def recorded_tool_calls(records: Iterable[JournalRecord]) -> list[RecordedToolCall]:
    """从 Journal 记录序列提取全部有结果的工具调用，按录制顺序。

    意图与结果按 ``intent_record_id`` 配对；附件取同 operation 的 ``function_call_output`` 对话项。
    没有结果的意图（进程死在调用途中）不进列表。
    """
    intents: dict[str, tuple[JournalRecord, ToolIntentCommittedV1]] = {}
    outcomes: dict[str, tuple[JournalRecord, ToolOutcomeCommittedV1]] = {}
    outputs: dict[str, tuple[ImageAttachmentV1, ...]] = {}
    for record in records:
        if record.record_type == "tool_intent_committed":
            intents[record.record_id] = (record, ToolIntentCommittedV1.model_validate(record.payload))
        elif record.record_type == "tool_outcome_committed":
            payload = ToolOutcomeCommittedV1.model_validate(record.payload)
            outcomes[payload.intent_record_id] = (record, payload)
        elif record.record_type == "conversation_item":
            item = ConversationItemV1.model_validate(record.payload)
            if item.item_kind == "function_call_output" and record.operation_id is not None:
                raw = item.payload.get("attachments")
                outputs[record.operation_id] = tuple(
                    ImageAttachmentV1.model_validate(a) for a in raw
                ) if isinstance(raw, list) else ()
    calls: list[RecordedToolCall] = []
    for intent_id, (intent_record, intent) in intents.items():
        found = outcomes.get(intent_id)
        if found is None:
            continue
        outcome_record, outcome = found
        calls.append(RecordedToolCall(
            intent_record_id=intent_id,
            call_id=intent.call_id,
            name=intent.name,
            arguments_hash=arguments_hash(dict(intent.effective_arguments)),
            status=str(outcome.status.value),
            output=outcome.output,
            attachments=outputs.get(intent_record.operation_id or "", ()),
            error_code=outcome.stable_error.code if outcome.stable_error is not None else None,
        ))
    return calls


@dataclass
class ToolReplayLedger:
    """回放期间的消费记录：哪些录制被用掉了，还剩多少。"""

    consumed: list[str] = field(default_factory=list)
    _pending: dict[tuple[str, str], list[RecordedToolCall]] = field(
        default_factory=lambda: defaultdict(list)
    )

    @property
    def remaining(self) -> int:
        """尚未被消费的录制调用数（回放结束仍 > 0 说明新运行少调了工具）。"""
        return sum(len(queue) for queue in self._pending.values())

    def take(self, name: str, arguments: dict[str, Any]) -> RecordedToolCall:
        """按（名字，参数）取下一次录制调用。

        Raises:
            ReplayDivergenceError: 没有同名同参的录制（执行路径分叉）。
        """
        queue = self._pending.get((name, arguments_hash(arguments)))
        if not queue:
            raise ReplayDivergenceError(
                f"no recorded call for tool {name!r} with these arguments "
                f"(remaining={self.remaining}, consumed={len(self.consumed)})"
            )
        call = queue.pop(0)
        self.consumed.append(call.intent_record_id)
        return call


KERNEL_TOOLS: frozenset[str] = frozenset({
    "call_skill", "read_skill", "search_skills", "spawn_skill", "kill_skill", "join_skill",
    "wait_peer", "wait_any", "await_skills", "send_message", "request_user_input",
})
"""内核编排工具：重放时照常运行（它们的效果——子 turn、句柄、消息——由内核自己重现），不从录制取结果。"""


def replay_tools(
    tools: Iterable[ToolSpec], calls: Iterable[RecordedToolCall],
) -> tuple[list[ToolSpec], ToolReplayLedger]:
    """把一组工具换成「从录制取结果、不执行」的版本；元数据（名字、schema、审计声明）原样保留。

    ``KERNEL_TOOLS`` 里的工具原样返回；台账只登记被替换工具的录制。

    Returns:
        替换后的工具列表与消费台账。
    """
    ledger = ToolReplayLedger()
    specs = list(tools)
    wrapped = {spec.name for spec in specs if spec.name not in KERNEL_TOOLS}
    for call in calls:
        if call.name in wrapped:
            ledger._pending[(call.name, call.arguments_hash)].append(call)  # noqa: SLF001
    replayed: list[ToolSpec] = []
    for spec in specs:
        if spec.name in wrapped:
            replayed.append(replace(spec, handler=_replay_handler(spec.name, ledger)))
        else:
            replayed.append(spec)
    return replayed, ledger


def _replay_handler(name: str, ledger: ToolReplayLedger) -> Any:
    """一个工具的回放 handler：查台账、还原结果。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx
        return ledger.take(name, dict(args)).result()

    return handler


__all__ = [
    "KERNEL_TOOLS",
    "RecordedToolCall",
    "ToolReplayLedger",
    "arguments_hash",
    "recorded_tool_calls",
    "replay_tools",
]
