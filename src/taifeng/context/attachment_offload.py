"""附件落盘与指针 —— 让被压缩淘汰的图片 / 文件「可恢复」（restorable-attachments，ADR 0147）。

压缩把附件从上下文拿走时，若只留下类型、大小与内容摘要，模型没有任何可执行的线索：
它知道「这里曾有一张图」，却没有办法再看一眼。本模块提供三件事，供多个压缩策略共用：

1. ``save_attachment`` —— 把附件原始字节写进工作区的 ``_offload/{thread_id}/`` 目录，
   文件名由内容摘要决定（同一附件多次淘汰只写一次）；
2. ``describe_attachment`` —— 一行描述，落盘成功时带 ``path=<相对路径>``；
3. ``collect_attachment_pointers`` —— 从一段历史里取回全部带路径的描述，供摘要型压缩
   原样保留（指针不经 LLM 转述，摘要再压缩一轮也不丢）。

参照（只学范式，不抄代码）：
    - Manus「restorable compression」—— 内容可以离开上下文，只要留下能取回它的路径 / URL；
    - DeepSeek Harness ``compaction-image-offload`` —— 占位文本写明附件与只读路径，
      恢复手段是读取工具而不是自动回填。
差异：taifeng 的落盘目录与 ``OffloadStrategy`` 共用（同一个 thread 级联清理入口），
路径按内容摘要寻址而非独立的附件服务（内核只依赖 ``WorkspaceFS`` 协议，ADR 0017 规则③）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from taifeng.context.placeholders import EVICTED_PREFIX
from taifeng.llm.attachment_refs import ATTACHMENT_PATH_PREFIX, is_ref_attachment, is_valid_ref

if TYPE_CHECKING:
    from collections.abc import Iterable

    from taifeng.conversation.models import ResponseItem
    from taifeng.tool.workspace import WorkspaceFS

logger = logging.getLogger(__name__)

# 落盘根目录 —— 与 OffloadStrategy 共用，thread 删除时一并清理
OFFLOAD_SUBDIR = "_offload"
# 附件文件名前缀：与工具输出落盘文件（以 call_id 命名）区分
_ATTACHMENT_FILE_PREFIX = "att-"
# 文件名里取多少位内容摘要（16 hex = 64 bit，thread 内碰撞可忽略）
_DIGEST_CHARS = 16
# MIME → 扩展名；未知类型一律 .bin（不猜）
_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}
# 摘要块的标签：handoff 把指针原样放进压缩条目，下一轮压缩再从这里取回
POINTER_BLOCK_OPEN = "<offloaded_attachments>"
POINTER_BLOCK_CLOSE = "</offloaded_attachments>"
# 一条指针里路径之后不再有内容：路径段只含这些字符，据此定位一条描述的结尾
_POINTER_RE = re.compile(r"path=((?:_offload/|attachment:)[^\s;\]]+)")
# 描述里会破坏「; 分隔、] 结尾」结构的字符，写入前替换
_DESCRIPTION_UNSAFE = str.maketrans({";": ",", "[": "(", "]": ")", "\n": " ", "\r": " "})


def is_safe_path_segment(name: object) -> bool:
    """单个路径段是否安全：非空、不为 . / ..、不含分隔符 / NUL、非绝对路径。

    ``thread_id`` 来自 history、``call_id`` 来自 provider —— 都是外部输入，直接拼进落盘
    路径会被 ``../../..`` 带出工作区（路径穿越）。
    """
    return (
        isinstance(name, str)
        and bool(name)
        and name not in {".", ".."}
        and "\x00" not in name
        and "/" not in name
        and "\\" not in name
        and not Path(name).is_absolute()
    )


def attachment_size(attachment: dict[str, Any]) -> int | None:
    """附件声明的字节数；缺失或非法返回 None（不猜）。"""
    size = attachment.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return size


def describe_attachment(attachment: dict[str, Any], *, path: str | None = None) -> str:
    """一个附件的一行描述：类型、大小、文件名（如有）、内容摘要前缀、落盘路径（如有）。"""
    media_type = attachment.get("media_type")
    parts = [media_type if isinstance(media_type, str) and media_type else "unknown type"]
    size = attachment_size(attachment)
    parts.append(f"{size / 1024:.1f}KB" if size is not None else "unknown size")
    filename = attachment.get("filename")
    if isinstance(filename, str) and filename:
        parts.append(filename.translate(_DESCRIPTION_UNSAFE))
    digest = attachment.get("sha256")
    if isinstance(digest, str) and digest:
        parts.append(f"sha256={digest[:8]}")
    if path:
        parts.append(f"path={path}")
    return " ".join(parts)


def _decode(attachment: dict[str, Any]) -> bytes | None:
    """取附件原始字节；正文缺失或不是合法 base64 返回 None。"""
    content = attachment.get("content")
    if not isinstance(content, str) or not content:
        return None
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return None


def _file_name(attachment: dict[str, Any], data: bytes) -> str:
    """按内容摘要派生落盘文件名；声明的摘要不合法时由字节现算。"""
    digest = attachment.get("sha256")
    if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
        digest = hashlib.sha256(data).hexdigest()
    media_type = attachment.get("media_type")
    extension = _EXTENSIONS.get(media_type, ".bin") if isinstance(media_type, str) else ".bin"
    return f"{_ATTACHMENT_FILE_PREFIX}{digest[:_DIGEST_CHARS]}{extension}"


async def _exists(workspace: WorkspaceFS, rel_path: str) -> bool:
    """目标是否已是一个文件（同一附件再次淘汰时不重复写）。"""
    try:
        return (await workspace.metadata(rel_path)).is_file
    except OSError:
        return False


async def save_attachment(
    workspace: WorkspaceFS, thread_id: str, attachment: dict[str, Any],
) -> str | None:
    """把附件原始字节写进工作区，返回相对路径；写不了返回 None。

    返回 None 的情形：``thread_id`` 不是安全路径段、附件没有可解码的正文、工作区解析或
    写入失败。调用方据此决定描述里是否带路径 —— 附件仍可照常淘汰，只是不可恢复
    （与引入本能力之前的行为一致），且每种情形都有 warning，不静默。

    Args:
        workspace: 与 ``file_read`` 共享的工作区。
        thread_id: 附件所属 thread（落盘按 thread 分目录）。
        attachment: 持久化形态的附件 dict（``ImageAttachmentV1`` / ``FileAttachmentV1``）。
    """
    if not is_safe_path_segment(thread_id):
        logger.warning("attachment offload rejected unsafe thread_id=%r", thread_id)
        return None
    data = _decode(attachment)
    if data is None:
        logger.warning("attachment offload skipped: no decodable content")
        return None
    rel_path = f"{OFFLOAD_SUBDIR}/{thread_id}/{_file_name(attachment, data)}"
    try:
        workspace.resolve(rel_path)
        if not await _exists(workspace, rel_path):
            await workspace.write_bytes(rel_path, data)
    except OSError as exc:
        # 解析与写入失败都只放弃落盘；取消异常不在此捕获
        logger.warning("attachment offload write failed for %s: %s", rel_path, exc)
        return None
    return rel_path


async def pointer_for(
    workspace: WorkspaceFS | None, thread_id: str, attachment: dict[str, Any],
) -> tuple[str | None, bool]:
    """附件离开上下文时留下的取回路径，以及是否为此新写了工作区文件。

    引用形态（ADR 0148）的原件已在附件存储里：路径是 ``attachment:<ref>``，不做任何 IO，
    与是否配置工作区无关。内联形态在配置了工作区时落盘（ADR 0147），否则没有路径。
    """
    ref = attachment.get("ref")
    if is_ref_attachment(attachment) and is_valid_ref(ref):
        return f"{ATTACHMENT_PATH_PREFIX}{ref}", False
    if workspace is None:
        return None, False
    path = await save_attachment(workspace, thread_id, attachment)
    return path, path is not None


async def remove_thread_offload_dir(workspace: WorkspaceFS, thread_id: str) -> None:
    """删除某 thread 的全部落盘文件（工具输出与附件同目录）；目录不存在视为成功。"""
    if not is_safe_path_segment(thread_id):
        logger.warning("offload cleanup rejected unsafe thread_id=%r", thread_id)
        return
    try:
        # 保留相对路径，让后端按最终链接条目删除，不能先 resolve 到链接目标。
        await workspace.remove(f"{OFFLOAD_SUBDIR}/{thread_id}", recursive=True)
    except FileNotFoundError:
        pass  # 已清理或从未产生落盘文件，视为幂等成功。


def _entry_with_path(candidate: str) -> str | None:
    """候选描述里有落盘路径时返回截到路径为止的描述，否则 None。"""
    match = _POINTER_RE.search(candidate)
    # 路径之后的内容（括号、提示语）不属于描述
    return candidate[: match.end()].strip() if match else None


def _evicted_entries(text: str) -> Iterable[str]:
    """从条目文本的 ``[evicted: ...]`` 行里取出每条带路径的附件描述。"""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(EVICTED_PREFIX):
            continue
        # 「[evicted: N attachment(s): 描述; 描述]」—— 去掉冒号前的计数
        _, _, described = stripped[len(EVICTED_PREFIX):].partition(": ")
        for candidate in described.split("; "):
            entry = _entry_with_path(candidate)
            if entry:
                yield entry


def _block_entries(summary: str) -> Iterable[str]:
    """上一轮压缩条目里 ``<offloaded_attachments>`` 块内的各条描述。"""
    start = summary.find(POINTER_BLOCK_OPEN)
    end = summary.find(POINTER_BLOCK_CLOSE)
    if start < 0 or end < start:
        return
    for line in summary[start + len(POINTER_BLOCK_OPEN): end].splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            entry = _entry_with_path(stripped[2:])
            if entry:
                yield entry


def collect_attachment_pointers(items: list[ResponseItem]) -> list[str]:
    """从一段历史里取回全部带路径的附件描述，按出现顺序、按路径去重。

    来源：``user_message`` / ``function_call_output`` 文本里的 ``[evicted: ...]`` 行，以及
    更早一轮压缩条目里的 ``<offloaded_attachments>`` 块 —— 所以指针能跨多轮摘要保留。
    """
    seen: dict[str, str] = {}
    for item in items:
        if item.kind == "user_message":
            entries = _evicted_entries(str(item.payload.get("text", "")))
        elif item.kind == "function_call_output":
            entries = _evicted_entries(str(item.payload.get("output", "")))
        elif item.kind == "compacted":
            entries = _block_entries(str(item.payload.get("summary", "")))
        else:
            continue
        for entry in entries:
            match = _POINTER_RE.search(entry)
            if match and match.group(1) not in seen:
                seen[match.group(1)] = entry
    return list(seen.values())


def format_pointer_block(pointers: list[str], *, limit: int) -> str:
    """把指针渲染成压缩条目里的一个块；超过 ``limit`` 时保留最近的若干条。

    Args:
        pointers: ``collect_attachment_pointers`` 的结果（旧 → 新）。
        limit: 最多保留多少条；``<= 0`` 或没有指针时返回空串（不生成空块）。
    """
    if limit <= 0 or not pointers:
        return ""
    kept = pointers[-limit:]
    lines = "\n".join(f"- {pointer}" for pointer in kept)
    return (
        f"{POINTER_BLOCK_OPEN}\n"
        "Attachments removed from context but still on disk. To look at an image again, "
        "read its path with the file reading tool:\n"
        f"{lines}\n{POINTER_BLOCK_CLOSE}"
    )


__all__ = [
    "OFFLOAD_SUBDIR",
    "POINTER_BLOCK_CLOSE",
    "POINTER_BLOCK_OPEN",
    "attachment_size",
    "collect_attachment_pointers",
    "describe_attachment",
    "format_pointer_block",
    "is_safe_path_segment",
    "pointer_for",
    "remove_thread_offload_dir",
    "save_attachment",
]
