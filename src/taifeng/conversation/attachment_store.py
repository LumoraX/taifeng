"""附件存储协议与本机实现（attachment-store，ADR 0148）。

附件字节交给 ``AttachmentStore``；会话条目里只留引用形态（``llm/attachment_refs.py``）。
写入「先存后记」：对象 durable 之后才把引用落进 transcript，所以不会出现「条目在、字节不在」。

协议只管存取，**完整性校验不在协议里**：内核的解析器用引用上的 ``sha256`` / ``size`` 统一
核对，后端作者不会因为漏做校验而留下静默损坏。

参照（只学范式，不抄代码）：
    - DeepSeek Harness ``ctx.attachments``：对象 durable 后才发布不可变的内容寻址引用，
      引用不透明，消费方不得据此推导路径；
    - Google ADK artifact service：上下文只放轻量描述，后端可换。
差异：没有图片规范化 / 按路由缩放，没有命名与版本；本机实现按内容摘要分目录。
业务侧落 S3 / DB 时自行实现本协议（R1 / ADR 0017 规则③），可用
``tests/conversation/attachment_store_conformance.py`` 验证。
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import anyio

from taifeng.llm.attachment_refs import ATTACHMENT_REF_PATTERN, is_ref_attachment

if TYPE_CHECKING:
    from collections.abc import Iterable

    from taifeng.conversation.models import ResponseItem

# 本机实现的对象布局版本目录；格式变化时换目录，不原地改写
_LAYOUT_DIR = "v1"
# 本机实现给出的引用前缀；消费方不得解析它
_LOCAL_REF_PREFIX = "sha256-"
_LOCAL_REF_RE = re.compile(r"^sha256-([0-9a-f]{64})$")
# 描述与压缩条目指针块里指向附件存储的写法（与 attachment_offload 的指针同源）
_REF_IN_TEXT_RE = re.compile(rf"path=attachment:({ATTACHMENT_REF_PATTERN})")


class AttachmentStoreError(Exception):
    """附件存储错误的基类。"""


class AttachmentNotFoundError(AttachmentStoreError):
    """引用在存储中不存在。"""


class AttachmentIntegrityError(AttachmentStoreError):
    """写入时声明的摘要与内容不符。"""


@runtime_checkable
class AttachmentStore(Protocol):
    """附件字节的独立存储。四个方法都必须可被 anyio 取消。"""

    async def put(self, data: bytes, *, media_type: str, sha256: str) -> str:
        """写入对象并返回引用；返回即 durable。

        相同内容 SHALL 返回相同引用且不重写已有对象。``sha256`` 与 ``data`` 不一致时
        SHALL 抛 ``AttachmentIntegrityError`` 且不写入。返回的引用只含
        ``[A-Za-z0-9._:-]``，长度 1–200。
        """
        ...

    async def get(self, ref: str) -> bytes:
        """读取对象；不存在抛 ``AttachmentNotFoundError``。"""
        ...

    async def exists(self, ref: str) -> bool:
        """对象是否存在。"""
        ...

    async def delete(self, ref: str) -> None:
        """删除对象；不存在为 noop。只供宿主的回收流程调用，内核不调用。"""
        ...


def _fsync_dir(path: Path) -> None:
    """把目录项（新建 / 改名）落盘；不支持目录 fsync 的平台（Windows）跳过。"""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalAttachmentStore:
    """本机目录实现：``<root>/v1/<摘要前 2 位>/<摘要>``，写临时文件后原子替换。

    引用为 ``sha256-<64 位 hex>``。不存扩展名与 MIME（它们在引用形态上）。
    阻塞 IO 全部放到工作线程；写入一旦开始便在线程里完成（取消只放弃等待），
    原子替换保证目标要么不存在、要么完整存在。
    """

    def __init__(self, root_dir: str | Path) -> None:
        """记录根目录；目录在首次写入时创建。"""
        self._root = Path(root_dir) / _LAYOUT_DIR

    @property
    def root(self) -> Path:
        """对象根目录（含布局版本）。"""
        return self._root

    def _path(self, ref: str) -> Path:
        """引用 → 对象路径；不是本实现给出的引用一律视为不存在。"""
        match = _LOCAL_REF_RE.match(ref) if isinstance(ref, str) else None
        if match is None:
            raise AttachmentNotFoundError(f"unknown attachment ref: {ref!r}")
        digest = match.group(1)
        return self._root / digest[:2] / digest

    async def put(self, data: bytes, *, media_type: str, sha256: str) -> str:
        """写入（幂等）；摘要不符抛 ``AttachmentIntegrityError``。"""
        del media_type  # 引用形态自带 MIME，本机实现不存
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise AttachmentIntegrityError("declared sha256 does not match attachment bytes")
        ref = f"{_LOCAL_REF_PREFIX}{actual}"
        target = self._path(ref)

        def write() -> None:
            if target.is_file():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                with tmp.open("wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
                _fsync_dir(target.parent)  # rename 本身也要落盘，否则掉电后引用可能指向空
            finally:
                tmp.unlink(missing_ok=True)

        await anyio.to_thread.run_sync(write)
        return ref

    async def get(self, ref: str) -> bytes:
        """读取；不存在抛 ``AttachmentNotFoundError``。"""
        path = self._path(ref)

        def read() -> bytes:
            try:
                return path.read_bytes()
            except FileNotFoundError:
                raise AttachmentNotFoundError(f"attachment not found: {ref}") from None

        return await anyio.to_thread.run_sync(read)

    async def exists(self, ref: str) -> bool:
        """对象是否存在；形状不认识的引用返回 False。"""
        try:
            path = self._path(ref)
        except AttachmentNotFoundError:
            return False
        return await anyio.to_thread.run_sync(path.is_file)

    async def delete(self, ref: str) -> None:
        """删除；不存在或形状不认识为 noop。"""
        try:
            path = self._path(ref)
        except AttachmentNotFoundError:
            return
        await anyio.to_thread.run_sync(lambda: path.unlink(missing_ok=True))


def _texts(item: ResponseItem) -> Iterable[str]:
    """条目里可能带指针的文本字段。"""
    for key in ("text", "output", "summary"):
        value = item.payload.get(key)
        if isinstance(value, str) and value:
            yield value


ATTACHMENT_REFS_METADATA_KEY = "attachment_refs"
"""条目 ``metadata`` 键：附件被驱逐 / 被摘要后，由压缩策略记下的引用列表（不在模型可见文本里）。"""


def thread_attachment_refs(items: Iterable[ResponseItem]) -> set[str]:
    """一段历史**确实持有**的附件引用：条目上的引用形态，以及压缩策略记在 metadata 里的。

    只认结构化来源，不扫文本——文本可以由模型、用户或工具输出随意写出，不能据此授权读取
    （``file_read`` 的归属校验用本函数）。
    """
    refs: set[str] = set()
    for item in items:
        raw: Any = item.payload.get("attachments")
        if isinstance(raw, list):
            refs.update(
                attachment["ref"] for attachment in raw
                if is_ref_attachment(attachment) and isinstance(attachment.get("ref"), str)
            )
        recorded = item.metadata.get(ATTACHMENT_REFS_METADATA_KEY)
        if isinstance(recorded, list):
            refs.update(ref for ref in recorded if isinstance(ref, str))
    return refs


def collect_attachment_refs(items: Iterable[ResponseItem]) -> set[str]:
    """一段历史里提到的全部附件引用，供宿主做回收（标记-清除）。

    比 ``thread_attachment_refs`` 宽：另外扫描文本里的 ``path=attachment:<ref>``（驱逐描述、
    压缩条目指针块）。用于回收时多算无害；**不要**用它做读取授权。内核自己从不删除存储对象。
    """
    items = list(items)
    refs = thread_attachment_refs(items)
    for item in items:
        for text in _texts(item):
            refs.update(_REF_IN_TEXT_RE.findall(text))
    return refs


__all__ = [
    "AttachmentIntegrityError",
    "AttachmentNotFoundError",
    "AttachmentStore",
    "AttachmentStoreError",
    "LocalAttachmentStore",
    "ATTACHMENT_REFS_METADATA_KEY",
    "collect_attachment_refs",
    "thread_attachment_refs",
]
