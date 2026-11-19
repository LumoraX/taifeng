"""附件引用的写入与解析（attachment-store，ADR 0148）。

三件事：

1. ``AttachmentResolver`` —— 每个 pool 一个：经存储取回字节并按引用上的 ``sha256`` / ``size``
   核对；按字节计的 LRU 缓存；同一引用的并发读取合并为一次后端调用。
2. **先存后记**：``externalize_user_attachments`` / ``externalize_tool_attachments`` 在 admission
   通过后把字节写进存储，返回引用形态的 payload（带从字节读出的宽高 / 页数）。
3. **解析阶段**：``materialize_history`` 在每次构建请求之前把引用换回内联形态，交给同步的
   请求构建函数；history 本身不被修改。取不回的历史附件降级为占位文本并发事件。

由引用换回的内联 dict 与原内联附件逐键相同，所以 provider 收到的请求逐字节相同（R2）。
"""

from __future__ import annotations

import base64
import collections
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio

from taifeng.conversation.attachment_store import (
    AttachmentIntegrityError,
    AttachmentNotFoundError,
)
from taifeng.llm.attachment_refs import (
    FileAttachmentRefV1,
    ImageAttachmentRefV1,
    has_ref_attachments,
    is_ref_attachment,
)
from taifeng.llm.client import model_capabilities as declared_capabilities
from taifeng.llm.errors import (
    AttachmentStoreRequiredError,
    AttachmentUnavailableError,
    UnsupportedModalityError,
)
from taifeng.llm.file_input import FileAttachmentV1, InspectedFile
from taifeng.llm.image_input import ImageAttachmentV1, InspectedImage, admit_image_attachments
from taifeng.loop.attachment_parts import inspect_user_attachments
from taifeng.loop.event_attachment import AttachmentStored, AttachmentUnavailable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taifeng.conversation.attachment_store import AttachmentStore
    from taifeng.conversation.models import ResponseItem
    from taifeng.llm.file_input import FileInputPolicy
    from taifeng.llm.image_input import ImageInputPolicy
    from taifeng.loop.event_base import _Msg

    Emit = Callable[[_Msg], Awaitable[None]]

DEFAULT_ATTACHMENT_CACHE_BYTES = 64 * 1024 * 1024
"""解析器缓存的默认上限（字节）；0 = 不缓存。"""

# 可带附件的条目类型 → 承载文本的 payload 键（降级占位文本追加在这里）
_TEXT_KEYS: dict[str, str] = {"user_message": "text", "function_call_output": "output"}


@dataclass
class _Inflight:
    """同一引用的并发读取共用的锁与等待者计数。"""

    lock: anyio.Lock
    waiters: int = 0


class AttachmentResolver:
    """经附件存储存取字节；读取时核对摘要与大小。

    Args:
        store: 注入的附件存储。
        cache_bytes: LRU 缓存上限（字节）；0 关闭缓存。单个对象超过上限时不缓存。

    Raises:
        ValueError: ``cache_bytes`` 为负。
    """

    def __init__(self, store: AttachmentStore, *, cache_bytes: int = DEFAULT_ATTACHMENT_CACHE_BYTES) -> None:
        if cache_bytes < 0:
            raise ValueError(f"attachment_cache_bytes must be non-negative, got {cache_bytes!r}")
        self._store = store
        self._capacity = cache_bytes
        self._cache: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._cached_bytes = 0
        self._inflight: dict[str, _Inflight] = {}

    @property
    def store(self) -> AttachmentStore:
        """注入的附件存储（宿主上传 / 回收用）。"""
        return self._store

    def _remember(self, ref: str, data: bytes) -> None:
        """放进 LRU 缓存；超过上限时从最久未用的开始淘汰。"""
        if len(data) > self._capacity:
            return
        old = self._cache.pop(ref, None)
        if old is not None:
            self._cached_bytes -= len(old)
        self._cache[ref] = data
        self._cached_bytes += len(data)
        while self._cached_bytes > self._capacity:
            _, evicted = self._cache.popitem(last=False)
            self._cached_bytes -= len(evicted)

    def _cached(self, ref: str) -> bytes | None:
        """命中则刷新最近使用顺序。"""
        data = self._cache.get(ref)
        if data is not None:
            self._cache.move_to_end(ref)
        return data

    async def fetch(self, ref: str, *, sha256: str, size: int) -> bytes:
        """取回并核对字节；不存在抛 ``AttachmentNotFoundError``，不符抛 ``AttachmentIntegrityError``。"""
        cached = self._cached(ref)
        if cached is not None:
            return cached
        slot = self._inflight.setdefault(ref, _Inflight(anyio.Lock()))
        slot.waiters += 1
        try:
            async with slot.lock:
                cached = self._cached(ref)
                if cached is not None:
                    return cached
                data = await self._store.get(ref)
                if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
                    raise AttachmentIntegrityError(f"attachment {ref} does not match its reference")
                self._remember(ref, data)
                return data
        finally:
            slot.waiters -= 1
            if slot.waiters == 0:
                self._inflight.pop(ref, None)

    async def save(self, data: bytes, *, media_type: str, sha256: str) -> str:
        """写入存储并放进缓存，返回引用。"""
        ref = await self._store.put(data, media_type=media_type, sha256=sha256)
        self._remember(ref, data)
        return ref


# ---- 形态互换 ----


def _inline_from_ref(attachment: dict[str, Any], data: bytes) -> dict[str, Any]:
    """引用形态 + 字节 → 与原内联附件逐键相同的 dict。"""
    content = base64.b64encode(data).decode("ascii")
    if attachment.get("kind") == "image":
        ref = ImageAttachmentRefV1.model_validate(attachment)
        return ImageAttachmentV1(
            media_type=ref.media_type, size=ref.size, sha256=ref.sha256,
            content=content, detail=ref.detail,
        ).model_dump()
    file_ref = FileAttachmentRefV1.model_validate(attachment)
    return FileAttachmentV1(
        media_type=file_ref.media_type, size=file_ref.size, sha256=file_ref.sha256,
        content=content, filename=file_ref.filename,
    ).model_dump()


def _ref_from_inspected(inspected: InspectedImage | InspectedFile, ref: str) -> dict[str, Any]:
    """已校验的内联附件 + 引用 → 引用形态 dict（元数据取自 admission 结果）。"""
    if isinstance(inspected, InspectedImage):
        image = inspected.attachment
        return ImageAttachmentRefV1(
            ref=ref, media_type=image.media_type, size=image.size, sha256=image.sha256,
            width=inspected.width, height=inspected.height, detail=image.detail,
        ).model_dump()
    file = inspected.attachment
    return FileAttachmentRefV1(
        ref=ref, media_type=file.media_type, size=file.size, sha256=file.sha256,
        pages=inspected.page_count, filename=file.filename,
    ).model_dump()


def _fetch_failure(exc: BaseException) -> str:
    """取回失败 → 事件里的 ``reason``。"""
    if isinstance(exc, AttachmentNotFoundError):
        return "not_found"
    if isinstance(exc, AttachmentIntegrityError):
        return "integrity_mismatch"
    return "backend_error"


async def _fetch_inline(resolver: AttachmentResolver, attachment: dict[str, Any]) -> dict[str, Any]:
    """取回一个引用形态附件并换成内联形态；形状非法按完整性错误处理。"""
    sha256, size = attachment.get("sha256"), attachment.get("size")
    ref = attachment.get("ref")
    if not (isinstance(ref, str) and isinstance(sha256, str) and isinstance(size, int)):
        raise AttachmentIntegrityError("attachment reference is malformed")
    data = await resolver.fetch(ref, sha256=sha256, size=size)
    return _inline_from_ref(attachment, data)


async def _resolve_strict(
    resolver: AttachmentResolver | None, attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把一组附件里的引用全部换成内联；任何一个取不回即抛 ``AttachmentUnavailableError``。"""
    if not any(is_ref_attachment(attachment) for attachment in attachments):
        return attachments
    if resolver is None:
        raise AttachmentStoreRequiredError("attachment reference given but no attachment store")
    resolved: list[dict[str, Any]] = []
    for attachment in attachments:
        if not is_ref_attachment(attachment):
            resolved.append(attachment)
            continue
        try:
            resolved.append(await _fetch_inline(resolver, attachment))
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:  # noqa: BLE001 —— 归类后如实抛给调用方
            raise AttachmentUnavailableError(
                f"attachment {attachment.get('ref')!r} unavailable: {_fetch_failure(exc)}"
            ) from exc
    return resolved


def _stored_event(origin: str, ref_payload: dict[str, Any]) -> AttachmentStored:
    """写入事件：只带元数据。"""
    return AttachmentStored(data={
        "origin": origin,
        "kind": ref_payload["kind"],
        "media_type": ref_payload["media_type"],
        "size": ref_payload["size"],
        "sha256": str(ref_payload["sha256"])[:8],
        "ref": ref_payload["ref"],
    })


async def _save_all(
    resolver: AttachmentResolver,
    inspected: list[InspectedImage | InspectedFile],
    *,
    origin: str,
    emit: Emit | None,
) -> list[dict[str, Any]]:
    """把已校验的附件逐个写入存储；任一失败抛 ``AttachmentUnavailableError``。"""
    refs: list[dict[str, Any]] = []
    for item in inspected:
        attachment = item.attachment
        data = base64.b64decode(attachment.content)
        try:
            ref = await resolver.save(
                data, media_type=attachment.media_type, sha256=attachment.sha256,
            )
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:  # noqa: BLE001 —— 写不进存储即拒绝，不回退为内联
            raise AttachmentUnavailableError(f"attachment store write failed: {exc}") from exc
        payload = _ref_from_inspected(item, ref)
        refs.append(payload)
        if emit is not None:
            await emit(_stored_event(origin, payload))
    return refs


# ---- 写入：先存后记 ----


async def externalize_user_attachments(
    attachments: list[dict[str, Any]],
    *,
    resolver: AttachmentResolver | None,
    image_input_policy: ImageInputPolicy,
    file_input_policy: FileInputPolicy,
    model_client: object,
    emit: Emit | None = None,
) -> list[dict[str, Any]]:
    """用户消息附件的准入与外置，返回将落进条目的附件列表。

    宿主传入的引用先取回字节并完整 admission（不采信其声明的元数据）；配置了存储时，
    全部附件在 durable append 之前写入存储并换成引用形态。未配置存储且没有引用时，
    行为与原入队准入完全相同（返回原列表）。

    Raises:
        AttachmentStoreRequiredError: 传了引用但未配置存储。
        AttachmentUnavailableError: 引用取不回，或写入存储失败。
        LLMError 子类: 能力门与 admission 的拒绝（与原入队准入相同）。
    """
    inline = await _resolve_strict(resolver, list(attachments))
    inspected = inspect_user_attachments(
        {"attachments": inline},
        image_input_policy=image_input_policy,
        file_input_policy=file_input_policy,
        model_capabilities=declared_capabilities(model_client),
    )
    if resolver is None:
        return list(attachments)
    return await _save_all(resolver, inspected, origin="user_message", emit=emit)


async def externalize_tool_attachments(
    attachments: tuple[Any, ...],
    *,
    resolver: AttachmentResolver | None,
    policy: ImageInputPolicy,
    emit: Emit | None = None,
) -> list[dict[str, Any]]:
    """工具结果图片附件的准入与外置（durable append 之前调用）。

    ``attachments`` 可混合 ``ImageAttachmentV1`` 与 ``ImageAttachmentRefV1``；引用先取回字节。

    Raises:
        AttachmentStoreRequiredError: 返回了引用但未配置存储。
        AttachmentUnavailableError: 引用取不回，或写入存储失败。
        UnsupportedModalityError: 策略未启用却返回了附件，或 MIME 不被允许。
        LLMError 子类: admission 的其余拒绝。
    """
    if not attachments:
        return []
    dumped = [attachment.model_dump() for attachment in attachments]
    inline = await _resolve_strict(resolver, dumped)
    if not policy.enabled:
        raise UnsupportedModalityError(
            "tool returned image attachments but image input policy is disabled"
        )
    images = [ImageAttachmentV1.model_validate(item) for item in inline]
    inspected = admit_image_attachments(images, policy)
    if resolver is None:
        return [image.model_dump() for image in images]
    return await _save_all(resolver, list(inspected), origin="tool_result", emit=emit)


# ---- 读取：解析阶段 ----


def _placeholder(attachment: dict[str, Any], reason: str) -> str:
    """取不回的附件在本次请求里的文本占位（不含正文）。"""
    size = attachment.get("size")
    size_text = f"{size / 1024:.1f}KB" if isinstance(size, int) else "unknown size"
    digest = str(attachment.get("sha256") or "")[:8]
    return (
        f"[attachment unavailable: {attachment.get('media_type') or 'unknown type'} {size_text} "
        f"sha256={digest} ref={attachment.get('ref')}: {reason}]"
    )


async def _materialize_item(
    item: ResponseItem, resolver: AttachmentResolver, emit: Emit | None,
) -> ResponseItem:
    """单个条目的引用 → 内联；取不回的换成占位文本并发事件。"""
    resolved: list[dict[str, Any]] = []
    notes: list[str] = []
    for attachment in item.payload.get("attachments") or []:
        if not is_ref_attachment(attachment):
            resolved.append(attachment)
            continue
        try:
            resolved.append(await _fetch_inline(resolver, attachment))
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:  # noqa: BLE001 —— 历史附件取不回降级，不毁 thread
            reason = _fetch_failure(exc)
            notes.append(_placeholder(attachment, reason))
            if emit is not None:
                await emit(AttachmentUnavailable(data={
                    "ref": attachment.get("ref"), "kind": attachment.get("kind"),
                    "media_type": attachment.get("media_type"), "item_id": item.id,
                    "reason": reason,
                }))
    payload = dict(item.payload)
    if resolved or item.kind == "user_message":
        payload["attachments"] = resolved
    else:
        payload.pop("attachments", None)
    if notes:
        key = _TEXT_KEYS[item.kind]
        text = str(payload.get(key) or "")
        payload[key] = "\n".join([text, *notes]) if text else "\n".join(notes)
    return item.model_copy(update={"payload": payload})


async def materialize_history(
    items: list[ResponseItem],
    resolver: AttachmentResolver | None,
    *,
    emit: Emit | None = None,
) -> list[ResponseItem]:
    """构建请求前把引用换回内联形态；不修改入参。没有引用时原样返回同一列表。

    Raises:
        AttachmentStoreRequiredError: history 里有引用但未配置存储（装配错误，不降级）。
    """
    if not any(item.kind in _TEXT_KEYS and has_ref_attachments(item.payload) for item in items):
        return items
    if resolver is None:
        raise AttachmentStoreRequiredError(
            "history contains attachment references but no attachment store is configured"
        )
    return [
        await _materialize_item(item, resolver, emit)
        if item.kind in _TEXT_KEYS and has_ref_attachments(item.payload) else item
        for item in items
    ]


async def materialize_strict(
    items: list[ResponseItem], resolver: AttachmentResolver | None,
) -> list[ResponseItem]:
    """与 ``materialize_history`` 相同，但任何附件取不回即抛（落盘前的复核用）。"""
    out: list[ResponseItem] = []
    for item in items:
        if item.kind in _TEXT_KEYS and has_ref_attachments(item.payload):
            payload = dict(item.payload)
            payload["attachments"] = await _resolve_strict(resolver, list(payload["attachments"]))
            item = item.model_copy(update={"payload": payload})
        out.append(item)
    return out


__all__ = [
    "DEFAULT_ATTACHMENT_CACHE_BYTES",
    "AttachmentResolver",
    "externalize_tool_attachments",
    "externalize_user_attachments",
    "materialize_history",
    "materialize_strict",
]
