"""服务端状态机与请求侦察 —— conformance 模拟器的「记账」环节。

参照 codex ``core/tests/common/responses.rs`` 的请求侦察断言面
（saw_function_call / function_call_output_text / message_input_texts 等）；
差异：codex 在 wiremock 线缆层记录 HTTP body JSON 并按 JSON 路径取值，
本实现于 ModelClient 协议层直接记录强类型 ``ApiRequest``，无需 JSON 解包。

两块职责：
- ``RequestLedger`` / ``RecordedRequest``：记录全部收到的请求，暴露测试断言面；
  ``violations`` 同时承接 contract 层的违规记录（D4 双保险——即使
  ``SimContractViolation`` 被引擎兜底捕获吞掉，fixture 收尾断言 violations 为空仍能红）。
- ``SimServerState``：token 记账（chars//4 确定性估算）+ ``context_window``
  超窗抛 ``ContextOverflowError`` + 最长公共前缀 cache 账本（量化 R2）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from taifeng.llm.errors import ContextOverflowError
from taifeng.llm.types import ImagePart, TextPart

if TYPE_CHECKING:
    from taifeng.llm.providers.sim.contract import SimContractViolation
    from taifeng.llm.types import ApiMessage, ApiRequest

# 与主流 provider 的经验比率同阶的确定性估算：4 字符 ≈ 1 token。
# 只承诺相对单调（请求变长估算必不减），不承诺与真实 tokenizer 绝对一致。
_CHARS_PER_TOKEN = 4

# 前缀 cache 账本的环形缓冲容量（模拟 provider 的有限前缀缓存池）
_PREFIX_CACHE_CAPACITY = 32


def _message_text(msg: ApiMessage) -> str:
    """取消息规范化文本；图片仅写结构摘要，绝不拼接 base64 正文。"""
    if isinstance(msg.content, str):
        return msg.content
    parts: list[str] = []
    for part in msg.content:
        if isinstance(part, TextPart):
            parts.append(part.text)
        elif isinstance(part, ImagePart):
            parts.append(
                f"<image media_type={part.media_type} detail={part.detail} "
                f"sha256={part.sha256}>"
            )
    return " ".join(parts)


def _canonical_text(request: ApiRequest) -> str:
    """请求的规范化全文串接：system_prompt + 各消息（role/正文/工具调用/核销 id）。

    作为 token 估算与前缀比较的统一基底——消息任何位置的变化（含 head）都会
    如实反映为前缀长度变化。
    """
    parts: list[str] = list(request.system_prompt)
    for msg in request.messages:
        parts.append(msg.role)
        parts.append(_message_text(msg))
        if msg.tool_call_id:
            parts.append(msg.tool_call_id)
        for tc in msg.tool_calls or []:
            parts.append(str(tc))
    return "\n".join(parts)


@dataclass(frozen=True)
class ImageInputDescriptor:
    """Sim 图片输入的脱敏结构描述；不包含 base64 或视觉语义。"""

    order: int
    message_index: int
    part_index: int
    media_type: str
    detail: str
    sha256: str


@dataclass(frozen=True)
class RecordedRequest:
    """单次采样请求的侦察视图（强类型 ApiRequest 的断言便捷层）。"""

    request: ApiRequest

    def image_inputs(self) -> tuple[ImageInputDescriptor, ...]:
        """按请求顺序返回图片结构描述，不暴露图片正文。"""
        descriptors: list[ImageInputDescriptor] = []
        for message_index, message in enumerate(self.request.messages):
            if isinstance(message.content, str):
                continue
            for part_index, part in enumerate(message.content):
                if not isinstance(part, ImagePart):
                    continue
                descriptors.append(
                    ImageInputDescriptor(
                        order=len(descriptors),
                        message_index=message_index,
                        part_index=part_index,
                        media_type=part.media_type,
                        detail=part.detail,
                        sha256=part.sha256,
                    )
                )
        return tuple(descriptors)

    def saw_function_call(self, call_id: str) -> bool:
        """本请求的 messages 中是否声明过该 call_id 的 tool_call。"""
        return any(
            str(tc.get("id", "")) == call_id
            for msg in self.request.messages
            for tc in (msg.tool_calls or [])
        )

    def function_call_output_text(self, call_id: str) -> str | None:
        """该 call_id 的工具结果正文；未核销返回 None。"""
        for msg in self.request.messages:
            if msg.role == "tool" and msg.tool_call_id == call_id:
                return _message_text(msg)
        return None

    def message_texts(self, role: str) -> list[str]:
        """指定 role 的全部消息正文（保序）。"""
        return [_message_text(m) for m in self.request.messages if m.role == role]

    def system_texts(self) -> list[str]:
        """system 文本全集：system_prompt 各段 + 中段 system 消息（保序）。"""
        return list(self.request.system_prompt) + self.message_texts("system")

    def tool_names(self) -> set[str]:
        """本请求注册的工具名集合。"""
        return {t.name for t in self.request.tools}

    def blob(self) -> str:
        """规范化全文（SimExpect.must_contain 的匹配基底）。"""
        return _canonical_text(self.request)


@dataclass
class RequestLedger:
    """请求台账：记录全部采样请求 + 承接违规记录（D4 双保险）。

    跨请求便捷方法（saw_function_call 等）扫描全部已记录请求；
    需要单请求精确断言时用 ``requests()[i]`` / ``last_request()`` 上的同名方法。
    """

    violations: list[SimContractViolation] = field(default_factory=list)
    _records: list[RecordedRequest] = field(default_factory=list)

    def record(self, request: ApiRequest) -> RecordedRequest:
        """登记一次采样请求，返回其侦察视图。"""
        rec = RecordedRequest(request=request)
        self._records.append(rec)
        return rec

    def requests(self) -> list[RecordedRequest]:
        """全部已记录请求（保序）。"""
        return list(self._records)

    def last_request(self) -> RecordedRequest | None:
        """最近一次请求；尚无请求返回 None。"""
        return self._records[-1] if self._records else None

    def single_request(self) -> RecordedRequest:
        """断言恰好发生过一次采样并返回之（数量不符直接 AssertionError）。"""
        assert len(self._records) == 1, f"期望恰好 1 次采样，实际 {len(self._records)} 次"
        return self._records[0]

    def saw_function_call(self, call_id: str) -> bool:
        """任一已记录请求中是否声明过该 call_id。"""
        return any(rec.saw_function_call(call_id) for rec in self._records)

    def function_call_output_text(self, call_id: str) -> str | None:
        """该 call_id 的工具结果正文（取最后一次出现）；从未核销返回 None。"""
        for rec in reversed(self._records):
            text = rec.function_call_output_text(call_id)
            if text is not None:
                return text
        return None

    def message_texts(self, role: str) -> list[str]:
        """最近一次请求中指定 role 的消息正文（无请求返回空列表）。"""
        last = self.last_request()
        return last.message_texts(role) if last else []

    def system_texts(self) -> list[str]:
        """最近一次请求的 system 文本全集（无请求返回空列表）。"""
        last = self.last_request()
        return last.system_texts() if last else []

    def tool_names(self) -> set[str]:
        """最近一次请求注册的工具名集合（无请求返回空集）。"""
        last = self.last_request()
        return last.tool_names() if last else set()

    def reset(self) -> None:
        """清空台账（违规与请求记录一并清）。"""
        self.violations.clear()
        self._records.clear()


@dataclass
class SimServerState:
    """服务端状态机：token 记账 + overflow + 前缀 cache 账本。

    - ``context_window=None``：不限窗（不抛 overflow）；
    - 前缀账本：保留近 ``_PREFIX_CACHE_CAPACITY`` 条已见请求的规范化串
      （环形缓冲），新请求与其中任一条的最长公共前缀折算 token 即 cache_read。
      前缀漂移不是错误，只如实反映为低 cache_read（R2 可量化断言）。
    """

    context_window: int | None = None
    last_cache_read: int = 0
    """最近一次 admit 折算的 cache_read（测试可观测：engine 消费事件后仍可断言）。"""
    last_cache_creation: int = 0
    """最近一次 admit 折算的 cache_creation。"""
    _seen_texts: list[str] = field(default_factory=list)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """chars//4 确定性 token 估算（相对单调，不承诺绝对值）。"""
        return len(text) // _CHARS_PER_TOKEN

    def admit(self, request: ApiRequest) -> tuple[int, int]:
        """请求准入：超窗抛 ``ContextOverflowError``；否则记账并返回 cache 计量。

        返回 ``(cache_read, cache_creation)``：与已见请求的最长公共前缀折算
        token 数、以及本请求新建缓存的余量 token 数。
        副作用：把本请求文本计入前缀账本（环形淘汰最旧）。
        """
        text = _canonical_text(request)
        total = self.estimate_tokens(text)
        if self.context_window is not None and total > self.context_window:
            raise ContextOverflowError(
                f"sim: 请求估算 {total} tokens 超出 context_window={self.context_window}"
            )
        prefix_chars = max(
            (_common_prefix_len(text, seen) for seen in self._seen_texts),
            default=0,
        )
        cache_read = prefix_chars // _CHARS_PER_TOKEN
        cache_creation = max(total - cache_read, 0)
        self.last_cache_read, self.last_cache_creation = cache_read, cache_creation
        self._seen_texts.append(text)
        if len(self._seen_texts) > _PREFIX_CACHE_CAPACITY:
            self._seen_texts.pop(0)
        return cache_read, cache_creation

    def reset(self) -> None:
        """清空前缀账本。"""
        self._seen_texts.clear()


def _common_prefix_len(a: str, b: str) -> int:
    """两串最长公共前缀长度（字符数）。"""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i
