"""JournalReplayClient —— 用录制的 SessionJournal 确定性回放 LLM 响应（journal-replay）。

strict audit 会话的 Journal 已经 durable 记下每次 LLM 调用的**请求**（安全投影
``llm_request_committed.api_request_safe`` + 完整摘要 ``canonical_attempt_sha256``）与
**最终响应**（``llm_response_committed.normalized_items`` / ``usage``）。本客户端把它们反过来用：
新一轮运行里每个请求按录制侧同一规则投影、匹配到录制的那次调用，把录制响应还原成事件流交回
内核——不触网、完全确定，可用于回归测试（「换了一版内核，同样的会话还走同一条路吗？」）。

匹配规则（``replay_match``，ADR 0054 / 0070）：
    - 按请求内容而非顺序匹配：并发 call_skill 子 turn 的请求顺序不稳定，内容能正确配对；
    - 两段式：采样 id 换成位置占位符后的安全投影定位候选，再把完整请求改写回录制采样 id、
      复算 ``canonical_attempt_sha256`` 逐字节复核——Responses 输入项里由 thread / submission
      派生的采样 id 每次运行不同，但从不进入 provider wire，一致重命名下相同即是同一请求；
    - 同一请求的多次录制按录制顺序依次消费；
    - 找不到匹配 → ``ReplayDivergenceError``（带剩余 / 已消费计数），这正是回归信号。

还原：
    - Chat 协议：``normalized_items``（reasoning / assistant / function_call）还原为 delta 事件；
      同一响应 batch 的会话项里若录有 provider 回传状态（reasoning 的 ``provider_reasoning``、
      function_call 的 ``extra_content``），一并以 ``reasoning_state`` / ``tool_call_done`` 还原；
    - Responses 协议：``normalized_items`` 原样经 ``normalized_output`` 交回，reasoning 的加密
      续传状态（``encrypted_content``）因此随之还原；
    - 只回放 ``status=complete`` 的响应；录制里的失败响应被消费时抛 ``ReplayUnsupportedError``。

参照：claw-code ``prompt_cache.rs`` 按请求哈希存取响应的做法；差异：数据源是审计 Journal，
不另建录制格式。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import TypeAdapter, ValidationError

from taifeng.llm.audit_redaction import project_attempt_request
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import LLMError
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    normalized_output,
    reasoning_delta,
    reasoning_state,
    server_model,
    text_delta,
    tool_call_done,
)
from taifeng.llm.providers.replay_match import (
    ReplayRequestShapeError,
    locator_digest,
    matches_recorded_digest,
)
from taifeng.llm.responses_types import NormalizedOutputItem, NormalizedRefusalItem
from taifeng.llm.types import TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from taifeng.conversation.journal.models import JournalRecord
    from taifeng.conversation.journal.records import (
        ConversationItemV1,
        LlmRequestCommittedV2,
        LlmResponseCommittedV1,
    )
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken

type ReplayProtocol = Literal["chat", "responses"]

# normalized item 的 Chat 协议 kind（与 llm/audit.py::_normalized_items 同源）
_CHAT_ITEM_KINDS = frozenset({"reasoning", "assistant", "function_call"})
_RESPONSES_ITEMS = TypeAdapter(list[NormalizedOutputItem])


class ReplayDivergenceError(LLMError):
    """回放时请求在录制中找不到对应调用——执行路径与录制分叉。"""

    kind = "replay_divergence"
    failure_class = "invalid_request"
    retryable = False


class ReplayUnsupportedError(LLMError):
    """录制内容本客户端无法回放（形状损坏 / 协议混杂 / 非 complete 响应）。"""

    kind = "replay_unsupported"
    failure_class = "invalid_request"
    retryable = False


@dataclass(frozen=True)
class RecordedCall:
    """一次录制的 LLM 调用（请求投影 + 摘要 + 最终响应 + 可还原的 provider 回传状态）。

    Attributes:
        digest: 录制的完整 ``canonical_attempt_sha256``（复核用）。
        api_request_safe: 录制的请求安全投影（定位用）。
        reasoning_state: Chat 协议该响应 reasoning 的 provider 回传状态（没有为 None）。
        tool_call_extras: Chat 协议该响应各 function_call 的 ``extra_content``（按 call_id）。
    """

    request_record_id: str
    provider: str
    model: str
    digest: str
    status: str
    normalized_items: tuple[dict[str, Any], ...]
    usage: dict[str, Any]
    api_request_safe: dict[str, Any]
    reasoning_state: dict[str, Any] | None = None
    tool_call_extras: dict[str, dict[str, Any]] = field(default_factory=dict)


def _thaw(value: object) -> Any:
    """把 Journal 冻结 JSON 容器复制成普通 dict / list（事件与 Pydantic 校验需要可变副本）。"""
    return json.loads(json.dumps(value))


def recorded_calls(records: Iterable[JournalRecord]) -> list[RecordedCall]:
    """从 Journal 记录中配出「请求 → 最终响应」对，按响应提交顺序返回。

    只取有 ``llm_response_committed`` 的调用（被重试掉的中间 attempt 不回放）；V1 请求记录
    没有摘要，跳过。Chat 协议的 provider 回传状态取自同一响应 batch 的会话项
    （``causation_id`` 指向该响应记录）。
    """
    from taifeng.conversation.journal.records import (
        ConversationItemV1,
        LlmRequestCommittedV2,
        LlmResponseCommittedV1,
        parse_llm_request_committed,
    )

    requests: dict[str, LlmRequestCommittedV2] = {}
    responses: list[tuple[str, LlmResponseCommittedV1]] = []
    items_by_source: dict[str, list[ConversationItemV1]] = defaultdict(list)
    for record in records:
        if record.record_type == "llm_request_committed":
            parsed = parse_llm_request_committed(record.payload)
            if isinstance(parsed, LlmRequestCommittedV2):
                requests[record.record_id] = parsed
        elif record.record_type == "llm_response_committed":
            responses.append(
                (record.record_id, LlmResponseCommittedV1.model_validate(record.payload))
            )
        elif record.record_type == "conversation_item" and record.causation_id:
            items_by_source[record.causation_id].append(
                ConversationItemV1.model_validate(record.payload)
            )
    calls: list[RecordedCall] = []
    for response_record_id, response in responses:
        request = requests.get(response.request_record_id)
        if request is not None:
            calls.append(_recorded_call(request, response, items_by_source[response_record_id]))
    return calls


def _recorded_call(
    request: LlmRequestCommittedV2,
    response: LlmResponseCommittedV1,
    items: list[ConversationItemV1],
) -> RecordedCall:
    """组装一次录制调用，并从同 batch 会话项取出 Chat 协议的 provider 回传状态。"""
    normalized: list[dict[str, Any]] = []
    for item in response.normalized_items:
        # 录制侧写入前已校验为 JSON 对象；读到非对象即录制损坏，显式拒绝
        if not isinstance(item, dict):
            raise ReplayUnsupportedError(
                f"recorded call {response.request_record_id} has a non-object item")
        normalized.append(_thaw(item))
    reasoning: dict[str, Any] | None = None
    extras: dict[str, dict[str, Any]] = {}
    for conversation in items:
        state = conversation.payload.get("provider_reasoning")
        extra = conversation.payload.get("extra_content")
        if conversation.item_kind == "reasoning" and isinstance(state, dict):
            reasoning = _thaw(state)
        elif conversation.item_kind == "function_call" and isinstance(extra, dict):
            extras[str(conversation.payload["call_id"])] = _thaw(extra)
    return RecordedCall(
        request_record_id=response.request_record_id,
        provider=request.provider,
        model=request.model,
        digest=request.canonical_attempt_sha256,
        status=str(response.status),
        normalized_items=tuple(normalized),
        usage=_thaw(response.usage),
        api_request_safe=_thaw(request.api_request_safe),
        reasoning_state=reasoning,
        tool_call_extras=extras,
    )


def _call_protocol(call: RecordedCall) -> ReplayProtocol | None:
    """由 normalized items 判别录制协议；无输出项时无法判别返回 None。

    Raises:
        ReplayUnsupportedError: 输出项既不是 Chat kind，也不是合法的 Responses terminal item。
    """
    items = call.normalized_items
    if not items:
        return None
    if all(item.get("kind") in _CHAT_ITEM_KINDS and "type" not in item for item in items):
        return "chat"
    try:
        parsed = _RESPONSES_ITEMS.validate_python(list(items))
    except ValidationError as exc:
        raise ReplayUnsupportedError(
            f"recorded call {call.request_record_id} has unrecognized output items") from exc
    if any(isinstance(item, NormalizedRefusalItem) for item in parsed):
        raise ReplayUnsupportedError(
            f"recorded call {call.request_record_id} contains a refusal item")
    return "responses"


def _default_capabilities(protocol: ReplayProtocol) -> ModelCapabilities:
    """按录制协议声明回放能力（Responses 需接受持久化的 provider state）。"""
    return ModelCapabilities(
        input_modalities=frozenset({"text"}),
        provider="replay",
        protocol=protocol,
        accepts_provider_state=protocol == "responses",
    )


@dataclass
class _Candidate:
    """按定位摘要分桶的一次待消费录制调用。"""

    call: RecordedCall
    sample_ids: tuple[str, ...]


class _ReplaySession:
    """单 turn 回放 session：取匹配的录制响应并按协议还原事件流。"""

    def __init__(self, client: JournalReplayClient, cancel: CancellationToken) -> None:
        self._client = client
        self._cancel = cancel

    async def __aenter__(self) -> _ReplaySession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """回放与 ``request`` 匹配的下一次录制调用。"""
        call = self._client._take(request)  # noqa: SLF001
        yield created()
        yield server_model(call.model)
        if self._client.protocol == "responses":
            events = _responses_events(call)
        else:
            events = _chat_events(call)
        for event in events:
            self._cancel.raise_if_cancelled()
            yield event
        yield completed(
            response_id=None,
            usage=TokenUsage.model_validate(call.usage) if call.usage else TokenUsage(),
            end_turn=not _has_calls(call),
            request_id=f"replay:{call.request_record_id}",
        )


def _has_calls(call: RecordedCall) -> bool:
    """录制响应是否含工具调用（决定 ``end_turn``）。"""
    return any(
        item.get("kind") == "function_call" or item.get("type") == "function_call"
        for item in call.normalized_items
    )


def _chat_events(call: RecordedCall) -> list[ResponseEvent]:
    """Chat 协议：delta 事件 + 录制的 provider 回传状态。"""
    events: list[ResponseEvent] = []
    for item in call.normalized_items:
        kind = item.get("kind")
        if kind == "reasoning" and item.get("text"):
            events.append(reasoning_delta(str(item["text"])))
        elif kind == "assistant" and item.get("text"):
            events.append(text_delta(str(item["text"])))
        elif kind == "function_call":
            call_id = str(item.get("call_id", ""))
            events.append(tool_call_done(
                call_id=call_id,
                name=str(item.get("name", "")),
                arguments=str(item.get("arguments", "")),
                extra_content=call.tool_call_extras.get(call_id),
            ))
    if call.reasoning_state is not None:
        events.append(reasoning_state(call.reasoning_state))
    return events


def _responses_events(call: RecordedCall) -> list[ResponseEvent]:
    """Responses 协议：可见文本 delta（供业务流式展示）+ 原样的 terminal normalized output。"""
    events: list[ResponseEvent] = []
    for item in call.normalized_items:
        if item.get("type") == "reasoning" and item.get("visible_text"):
            events.append(reasoning_delta(str(item["visible_text"])))
        elif item.get("type") == "message" and item.get("text"):
            events.append(text_delta(str(item["text"])))
    events.append(normalized_output([_thaw(item) for item in call.normalized_items]))
    return events


class JournalReplayClient:
    """用录制 Journal 回放 LLM 响应的 ModelClient（Chat / Responses 协议）。"""

    def __init__(
        self,
        calls: Iterable[RecordedCall],
        *,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        """
        Args:
            calls: ``recorded_calls(records)`` 的结果。
            capabilities: 回放 client 声明的能力；None 时按录制协议推断（text 输入）。录制时
                client 若声明了图片等能力（影响 prompt 组装），须显式传入同样的声明。

        Raises:
            ReplayUnsupportedError: 录制形状损坏、协议混杂，或与显式 ``capabilities`` 协议不符。
        """
        self._pending: dict[str, list[_Candidate]] = defaultdict(list)
        self._keys: list[tuple[str, str]] = []
        protocols: set[ReplayProtocol] = set()
        for call in calls:
            protocol = _call_protocol(call)
            if protocol is not None:
                protocols.add(protocol)
            try:
                key, sample_ids = locator_digest(call.provider, call.model, call.api_request_safe)
            except ReplayRequestShapeError as exc:
                raise ReplayUnsupportedError(
                    f"recorded call {call.request_record_id} has no replayable request") from exc
            self._pending[key].append(_Candidate(call, sample_ids))
            if (call.provider, call.model) not in self._keys:
                self._keys.append((call.provider, call.model))
        self.protocol = self._resolve_protocol(protocols, capabilities)
        self.capabilities = capabilities or _default_capabilities(self.protocol)
        self.consumed: list[str] = []

    @staticmethod
    def _resolve_protocol(
        protocols: set[ReplayProtocol], capabilities: ModelCapabilities | None,
    ) -> ReplayProtocol:
        """录制协议必须唯一，且与显式能力声明一致；录制无可判别输出项时按 Chat。"""
        if len(protocols) > 1:
            raise ReplayUnsupportedError("recording mixes chat and responses protocols")
        declared = capabilities.protocol if capabilities is not None else None
        inferred = next(iter(protocols), None)
        if declared is not None and declared not in ("chat", "responses"):
            raise ReplayUnsupportedError(f"unsupported replay protocol: {declared!r}")
        if inferred is not None and declared is not None and declared != inferred:
            raise ReplayUnsupportedError(
                f"capabilities declare {declared!r} but the recording is {inferred!r}")
        resolved = inferred or declared or "chat"
        return "responses" if resolved == "responses" else "chat"

    @classmethod
    def from_records(
        cls,
        records: Iterable[JournalRecord],
        *,
        capabilities: ModelCapabilities | None = None,
    ) -> JournalReplayClient:
        """由 Journal 记录序列（如 ``JsonlSessionJournalCore.load`` 的结果）构造。"""
        return cls(recorded_calls(records), capabilities=capabilities)

    @property
    def remaining(self) -> int:
        """尚未被消费的录制调用数（回放结束仍 > 0 说明新运行少走了调用）。"""
        return sum(len(queue) for queue in self._pending.values())

    def session(self, *, cancel: CancellationToken, model: str | None = None) -> _ReplaySession:
        """创建 turn 级回放 session。"""
        return _ReplaySession(self, cancel)

    def _take(self, request: ApiRequest) -> RecordedCall:
        """按两段式匹配取下一次录制调用。

        摘要随录制时的 provider / model 计算——逐一尝试录制里出现过的组合；空模型与录制侧
        ``AttemptObservableClientAdapter`` 同规则以录制模型补齐（子 skill 常不声明 model）。

        Raises:
            ReplayDivergenceError: 无匹配（执行路径分叉），含「仅脱敏内容 / 采样归组不同」。
            ReplayUnsupportedError: 匹配到的录制响应不是 complete。
        """
        near_miss = False
        for provider, model in self._keys:
            effective = request if request.model else request.model_copy(update={"model": model})
            safe = project_attempt_request(provider, model, effective).api_request_safe
            queue = self._pending.get(locator_digest(provider, model, safe)[0])
            if not queue:
                continue
            full = effective.model_dump(mode="json")
            for candidate in queue:
                if matches_recorded_digest(
                    provider, model, full, candidate.sample_ids, candidate.call.digest
                ):
                    queue.remove(candidate)
                    return self._consume(candidate.call)
            near_miss = True
        detail = "redacted content or sample grouping differs; " if near_miss else ""
        raise ReplayDivergenceError(
            f"request has no recorded counterpart ({detail}remaining={self.remaining}, "
            f"consumed={len(self.consumed)})")

    def _consume(self, call: RecordedCall) -> RecordedCall:
        """记账并拒绝回放非 complete 的录制响应。"""
        if call.status != "complete":
            raise ReplayUnsupportedError(
                f"recorded call {call.request_record_id} ended with status={call.status}")
        self.consumed.append(call.request_record_id)
        return call


__all__ = [
    "JournalReplayClient",
    "RecordedCall",
    "ReplayDivergenceError",
    "ReplayUnsupportedError",
    "recorded_calls",
]
