"""JournalReplayClient —— 用录制的 SessionJournal 确定性回放 LLM 响应（journal-replay）。

strict audit 会话的 Journal 已经 durable 记下每次 LLM 调用的**请求摘要**
（``llm_request_committed.canonical_attempt_sha256``）与**最终响应**
（``llm_response_committed.normalized_items`` / ``usage``）。本客户端把它们反过来用：
新一轮运行里每个请求先算同一种摘要，按摘要找到录制的那次调用，把录制响应还原成
事件流交回内核——不触网、完全确定，可用于回归测试（「换了一版内核，同样的会话还
走同一条路吗？」）。

匹配规则：
    - 按请求摘要匹配而非按顺序：并发 call_skill 子 turn 的请求顺序不稳定，摘要能正确配对；
    - 同摘要的多次录制按录制顺序依次消费；
    - 找不到匹配 → ``ReplayDivergenceError``（带请求摘要与剩余未消费录制），这正是回归信号。

边界：
    - 只回放 Chat 协议录制（``normalized_items`` 为 reasoning / assistant / function_call）。
      Responses 协议的输入项带由 thread id 派生的 sample id，每次运行都不同，摘要无法复现；
      遇到这类录制在构造期显式拒绝。
    - 只回放 ``status=complete`` 的响应；录制里的失败响应被消费时抛 ``ReplayUnsupportedError``。
    - provider 专有回传状态（thinking 签名、Gemini ``extra_content``）不在 normalized items 里，
      回放不还原。

参照：claw-code ``prompt_cache.rs`` 按请求哈希存取响应的做法；差异：数据源是审计 Journal，
不另建录制格式。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taifeng.llm.audit_redaction import project_attempt_request
from taifeng.llm.client import ModelCapabilities
from taifeng.llm.errors import LLMError
from taifeng.llm.events import (
    ResponseEvent,
    completed,
    created,
    reasoning_delta,
    server_model,
    text_delta,
    tool_call_done,
)
from taifeng.llm.types import TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from taifeng.conversation.journal.models import JournalRecord
    from taifeng.llm.types import ApiRequest
    from taifeng.loop.cancellation import CancellationToken

# normalized item 的 Chat 协议 kind（与 llm/audit.py::_normalized_items 同源）
_CHAT_ITEM_KINDS = frozenset({"reasoning", "assistant", "function_call"})


class ReplayDivergenceError(LLMError):
    """回放时请求在录制中找不到对应调用——执行路径与录制分叉。"""

    kind = "replay_divergence"
    failure_class = "invalid_request"
    retryable = False


class ReplayUnsupportedError(LLMError):
    """录制内容本客户端无法回放（Responses 协议 / 非 complete 响应）。"""

    kind = "replay_unsupported"
    failure_class = "invalid_request"
    retryable = False


@dataclass(frozen=True)
class RecordedCall:
    """一次录制的 LLM 调用（请求摘要 + 最终响应）。"""

    request_record_id: str
    provider: str
    model: str
    digest: str
    status: str
    normalized_items: tuple[dict[str, Any], ...]
    usage: dict[str, Any]


def recorded_calls(records: Iterable[JournalRecord]) -> list[RecordedCall]:
    """从 Journal 记录中配出「请求摘要 → 最终响应」对，按响应提交顺序返回。

    只取有 ``llm_response_committed`` 的调用（被重试掉的中间 attempt 不回放）；
    V1 请求记录没有摘要，跳过。
    """
    from taifeng.conversation.journal.records import (
        LlmRequestCommittedV2,
        LlmResponseCommittedV1,
        parse_llm_request_committed,
    )

    requests: dict[str, LlmRequestCommittedV2] = {}
    calls: list[RecordedCall] = []
    for record in records:
        if record.record_type == "llm_request_committed":
            parsed = parse_llm_request_committed(record.payload)
            if isinstance(parsed, LlmRequestCommittedV2):
                requests[record.record_id] = parsed
        elif record.record_type == "llm_response_committed":
            response = LlmResponseCommittedV1.model_validate(record.payload)
            request = requests.get(response.request_record_id)
            if request is None:
                continue
            items: list[dict[str, Any]] = []
            for item in response.normalized_items:
                # 录制侧写入前已校验为 JSON 对象；读到非对象即录制损坏，显式拒绝
                if not isinstance(item, dict):
                    raise ReplayUnsupportedError(
                        f"recorded call {response.request_record_id} has a non-object item")
                items.append(dict(item))
            calls.append(RecordedCall(
                request_record_id=response.request_record_id,
                provider=request.provider,
                model=request.model,
                digest=request.canonical_attempt_sha256,
                status=str(response.status),
                normalized_items=tuple(items),
                usage=dict(response.usage),
            ))
    return calls


class _ReplaySession:
    """单 turn 回放 session：按请求摘要取录制响应并还原事件流。"""

    def __init__(self, client: JournalReplayClient, cancel: CancellationToken) -> None:
        self._client = client
        self._cancel = cancel

    async def __aenter__(self) -> _ReplaySession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def stream(self, request: ApiRequest) -> AsyncIterator[ResponseEvent]:
        """回放与 ``request`` 摘要一致的下一次录制调用。"""
        call = self._client._take(request)  # noqa: SLF001
        yield created()
        yield server_model(call.model)
        for item in call.normalized_items:
            self._cancel.raise_if_cancelled()
            kind = item.get("kind")
            if kind == "reasoning" and item.get("text"):
                yield reasoning_delta(str(item["text"]))
            elif kind == "assistant" and item.get("text"):
                yield text_delta(str(item["text"]))
            elif kind == "function_call":
                yield tool_call_done(
                    call_id=str(item.get("call_id", "")),
                    name=str(item.get("name", "")),
                    arguments=str(item.get("arguments", "")),
                )
        has_calls = any(item.get("kind") == "function_call" for item in call.normalized_items)
        yield completed(
            response_id=None,
            usage=TokenUsage.model_validate(call.usage) if call.usage else TokenUsage(),
            end_turn=not has_calls,
            request_id=f"replay:{call.request_record_id}",
        )


class JournalReplayClient:
    """用录制 Journal 回放 LLM 响应的 ModelClient（Chat 协议）。"""

    def __init__(self, calls: Iterable[RecordedCall]) -> None:
        """
        Args:
            calls: ``recorded_calls(records)`` 的结果。

        Raises:
            ReplayUnsupportedError: 含 Responses 协议录制（无法复现请求摘要）。
        """
        self._pending: dict[str, deque[RecordedCall]] = defaultdict(deque)
        self._keys: list[tuple[str, str]] = []
        for call in calls:
            if any(item.get("kind") not in _CHAT_ITEM_KINDS for item in call.normalized_items):
                raise ReplayUnsupportedError(
                    f"recorded call {call.request_record_id} is not a chat-protocol response")
            self._pending[call.digest].append(call)
            key = (call.provider, call.model)
            if key not in self._keys:
                self._keys.append(key)
        self.consumed: list[str] = []
        self.capabilities = ModelCapabilities(
            input_modalities=frozenset({"text"}), provider="replay", protocol="chat")

    @classmethod
    def from_records(cls, records: Iterable[JournalRecord]) -> JournalReplayClient:
        """由 Journal 记录序列（如 ``JsonlSessionJournalCore.load`` 的结果）构造。"""
        return cls(recorded_calls(records))

    @property
    def remaining(self) -> int:
        """尚未被消费的录制调用数（回放结束仍 > 0 说明新运行少走了调用）。"""
        return sum(len(queue) for queue in self._pending.values())

    def session(self, *, cancel: CancellationToken, model: str | None = None) -> _ReplaySession:
        """创建 turn 级回放 session。"""
        return _ReplaySession(self, cancel)

    def _take(self, request: ApiRequest) -> RecordedCall:
        """按请求摘要取下一次录制调用。

        摘要随录制时的 provider / model 计算——逐一尝试录制里出现过的组合。

        Raises:
            ReplayDivergenceError: 无匹配（执行路径分叉）。
            ReplayUnsupportedError: 匹配到的录制响应不是 complete。
        """
        for provider, model in self._keys:
            # 与录制侧 AttemptObservableClientAdapter 同规则：请求未指定模型时以
            # client 默认模型补齐后再算摘要（子 skill 常不声明 model）
            effective = request if request.model else request.model_copy(update={"model": model})
            digest = project_attempt_request(provider, model, effective).canonical_attempt_sha256
            queue = self._pending.get(digest)
            if queue:
                call = queue.popleft()
                if call.status != "complete":
                    raise ReplayUnsupportedError(
                        f"recorded call {call.request_record_id} ended with status={call.status}")
                self.consumed.append(call.request_record_id)
                return call
        raise ReplayDivergenceError(
            f"request has no recorded counterpart (remaining={self.remaining}, "
            f"consumed={len(self.consumed)})")


__all__ = [
    "JournalReplayClient",
    "RecordedCall",
    "ReplayDivergenceError",
    "ReplayUnsupportedError",
    "recorded_calls",
]
