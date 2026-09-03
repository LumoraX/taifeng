"""OffloadStrategy —— 无损可回溯压缩档(压缩谱系第四档)。

参照(只学范式,不抄代码):
    - deepagents libs/deepagents/.../middleware/_message_eviction.py —— 超大 tool
      结果落盘 + stub 指针 + head/tail 预览;LLM 按需 read_file 分页回读。
    - 差异 Y:以 taifeng CompressionStrategy 协议 + 文件沙箱原语重写,挂进
      CompressionOrchestrator;回溯一律 LLM 主动 file_read(不自动 rehydrate)。

与谱系内其他档的关系(见 change ``compaction-offload-strategy`` design D4):
    orchestrator 是「首个 should_trigger 命中的策略胜出、单轮只跑一个」。本策略
    priority 高于 SurgicalTrim,且 should_trigger 是**选择性**的——仅当 tail 中存在
    超阈值、未落盘、有配对的 tool 结果时才命中;否则返回 None 让位给有损档。
    由此实现「有大结果就优先无损落盘,否则退化到 trim/摘要」。

R1-R5(见 design):
    - R1:仅 stdlib + anyio + 文件沙箱;无业务概念。
    - R2:仅改写 anchor **之后**(index > cache_anchor_index)的 tail 条目,
      ``cache_invalidated`` 恒为 False,``anchor_preserved_until`` 不前移。
    - R3:offload 计数落 ``CompressionResult.detail``,由 turn 组装
      ``compaction_completed`` 事件透传(与 surgical_trim 同机制)。
    - R4:CompressionContext 不携带 CancellationToken;协作式取消——每条落盘前
      ``await anyio.lowlevel.checkpoint()``,外部 task / CancelScope 取消在此生效。
    - R5:stub 落 JSONL;落盘路径按 ``_offload/{thread_id}/{call_id}`` 确定性派生,
      offload 文件独立持久化 → replay 重建 stub 后 file_read 可回读原文。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from taifeng.context.compressor import (
    CompressionContext,
    CompressionResult,
    CompressionTrigger,
)
from taifeng.context.placeholders import OFFLOAD_PREFIX, is_placeholder

if TYPE_CHECKING:
    from taifeng.context.injection import InitialContextInjection
    from taifeng.conversation.models import ResponseItem

# offload 文件根下的固定子目录名 —— 与业务文件区隔离
_OFFLOAD_SUBDIR = "_offload"

logger = logging.getLogger(__name__)


def _is_safe_segment(name: str) -> bool:
    """单个路径段是否安全：非空、不为 . / ..、不含分隔符 / NUL、非绝对路径。

    ``call_id`` 来自 provider 返回、``thread_id`` 来自 history —— 二者都是外部输入，
    直接拼进落盘路径会被 ``../../..`` 带出 file_root（路径穿越）。
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


def _build_name_index(history: list[ResponseItem]) -> dict[str, str]:
    """call_id → tool name 索引(function_call_output 自身不带 name,需回溯配对)。

    无配对 function_call 的 output 即孤儿,不在索引内 → 不 offload(不猜测)。
    """
    return {
        it.payload["call_id"]: it.payload["name"]
        for it in history
        if it.kind == "function_call"
    }


def _build_preview(text: str, head_lines: int, tail_lines: int) -> str:
    """构造 head + tail 行预览,中段以 ``... [N lines truncated] ...`` 标记。"""
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines:
        return "\n".join(lines)
    head = lines[:head_lines]
    tail = lines[-tail_lines:] if tail_lines else []
    omitted = len(lines) - head_lines - tail_lines
    return "\n".join([*head, f"... [{omitted} lines truncated] ...", *tail])


def _rewrite_output(item: ResponseItem, new_output: str) -> ResponseItem:
    """就地替换 output payload —— 保留 item 其余字段(id / thread_id,R5 身份不变)。"""
    payload = dict(item.payload)
    payload["output"] = new_output
    return item.model_copy(update={"payload": payload})


class OffloadStrategy:
    """超大 tool 结果落盘 + stub 替换的无损可回溯压缩策略。

    Args:
        file_root: offload 文件沙箱根。落盘到 ``{file_root}/_offload/{thread_id}/{call_id}``;
            业务侧应与 file_read 工具的 ``root_dir`` 同根,stub 中给出的相对路径方可回读。
        priority: orchestrator 排序优先级。默认 30,高于 SurgicalTrim(20)——
            有大结果时优先无损落盘而非有损剪枝。
        offload_bytes_threshold: 单条 output 字节数 ≥ 此值才落盘(独立阈值,OQ2)。
        preview_head_lines / preview_tail_lines: stub 中 head/tail 预览行数。
    """

    name = "offload"

    def __init__(
        self,
        *,
        file_root: str | Path,
        priority: int = 30,
        offload_bytes_threshold: int = 8 * 1024,
        preview_head_lines: int = 5,
        preview_tail_lines: int = 5,
    ) -> None:
        self.priority = priority
        self._root = Path(file_root)
        self._threshold = offload_bytes_threshold
        self._head_lines = preview_head_lines
        self._tail_lines = preview_tail_lines

    # ---- 触发 ----

    def should_trigger(self, ctx: CompressionContext) -> CompressionTrigger | None:
        """tail 中存在超阈值、未落盘、有配对的 tool 结果时命中;pre_turn 永不命中。"""
        # spec D5:pre_turn 不 offload(只动 tail,不碰 head cached 区)
        if ctx.phase == "pre_turn":
            return None
        history = list(ctx.history)
        names = _build_name_index(history)
        if self._eligible_indices(history, names, ctx.cache_anchor_index):
            return CompressionTrigger(reason="tool_overflow", threshold_pct=1.0)
        return None

    # ---- 可 offload 判定 ----

    def _eligible_indices(
        self,
        history: list[ResponseItem],
        names: dict[str, str],
        anchor: int,
    ) -> list[int]:
        """anchor 之后(index > anchor)、超阈值、非占位符、有配对的 output 索引。

        ``cache_anchor_index``(含)及之前已 cached(R2),首个可变索引 = anchor+1。
        排除:孤儿 output(call_id 回溯不到配对 fc)、已是占位符的条目(幂等)、
        未达阈值的小结果。
        """
        picked: list[int] = []
        start = max(anchor + 1, 0)
        for i in range(start, len(history)):
            it = history[i]
            if it.kind != "function_call_output":
                continue
            if it.payload["call_id"] not in names:
                continue
            # 图片无法无损回溯：base64 落盘后 file_read 回来是文本，模型看不见。
            # 本策略的定位是「无损可回溯」，兑现不了就不该接手——交给诚实有损的
            # SurgicalTrim（它会连图一起剪并留计数痕迹）。
            if it.payload.get("attachments"):
                continue
            output = it.payload["output"]
            if is_placeholder(output):
                continue
            if len(output.encode("utf-8")) < self._threshold:
                continue
            picked.append(i)
        return picked

    # ---- 主入口 ----

    async def compress(
        self,
        ctx: CompressionContext,
        injection: InitialContextInjection,  # noqa: ARG002 — offload 不依赖注入语义
    ) -> CompressionResult:
        """落盘超阈值 tool 结果并以 stub 替换;只动 anchor 之后的 tail。

        取消(R4):每条落盘前 ``await anyio.lowlevel.checkpoint()`` 协作检查点。
        失败回退:某条落盘抛 OSError → 保留其原始 output,继续处理其余候选。
        """
        history = list(ctx.history)
        names = _build_name_index(history)
        offloaded = 0
        bytes_saved = 0

        for i in self._eligible_indices(history, names, ctx.cache_anchor_index):
            it = history[i]
            call_id = it.payload["call_id"]
            output = it.payload["output"]
            nbytes = len(output.encode("utf-8"))
            # R4:落盘前协作取消检查点(外部 task / CancelScope 取消在此生效)
            await anyio.lowlevel.checkpoint()
            stub = await self._offload_one(ctx, call_id, output, nbytes)
            if stub is None:
                continue  # 落盘失败 → 保留原始 output
            history[i] = _rewrite_output(it, stub)
            offloaded += 1
            bytes_saved += nbytes

        detail = {"offloaded": offloaded, "bytes_saved": bytes_saved}
        if offloaded == 0:
            return CompressionResult(
                success=False,
                cache_invalidated=False,
                anchor_preserved_until=ctx.cache_anchor_index,
                reason="nothing_to_offload",
                detail=detail,
            )
        # R2:只动 tail → cache 未失效,anchor 不前移
        return CompressionResult(
            success=True,
            cache_invalidated=False,
            anchor_preserved_until=ctx.cache_anchor_index,
            new_history=history,
            removed_item_count=offloaded,
            detail=detail,
        )

    # ---- 生命周期(thread 级联清理)----

    async def cleanup_thread(self, thread_id: str) -> None:
        """删除某 thread 的全部 offload 文件(thread/conversation 删除时调用)。

        v1 仅做 thread 级联清理,不做 TTL / 容量上限。目标目录不存在时为 noop(幂等)。
        业务侧在销毁 thread 的钩子里调用本方法,与 history 删除对齐。
        """
        tdir = anyio.Path(self._root) / _OFFLOAD_SUBDIR / thread_id
        if await tdir.exists():
            # 递归删除该 thread 目录下全部 offload 文件(阻塞 IO 下沉线程池)
            await anyio.to_thread.run_sync(shutil.rmtree, Path(tdir))

    # ---- 落盘单条 ----

    async def _offload_one(
        self,
        ctx: CompressionContext,
        call_id: str,
        output: str,
        nbytes: int,
    ) -> str | None:
        """落盘单条 output,返回 stub 文本;落盘失败返回 None(调用方保留原文)。"""
        thread_id = self._thread_id_for(ctx, call_id)
        if not (_is_safe_segment(thread_id) and _is_safe_segment(call_id)):
            # 路径穿越型 id：不落盘、保留原文，与 OSError 分支同语义
            # （非 silent fallback：数据不被改写，且有 warning）
            logger.warning(
                "offload rejected unsafe path segment: thread_id=%r call_id=%r",
                thread_id, call_id,
            )
            return None
        rel_path = f"{_OFFLOAD_SUBDIR}/{thread_id}/{call_id}"
        thread_dir = Path(self._root) / _OFFLOAD_SUBDIR / thread_id
        target_path = thread_dir / call_id
        # 二次证明：resolve 后的父目录必须仍是 thread 目录（防 symlink 等绕过）
        if target_path.resolve().parent != thread_dir.resolve():
            logger.warning("offload target escaped thread dir: %s", target_path)
            return None
        target = anyio.Path(target_path)
        try:
            await target.parent.mkdir(parents=True, exist_ok=True)
            await target.write_text(output, encoding="utf-8")
        except OSError:
            # 系统级落盘失败 —— 不静默丢数据,交由调用方保留原始条目
            return None
        preview = _build_preview(output, self._head_lines, self._tail_lines)
        # stub 必须以 OFFLOAD_PREFIX 起头,使 is_placeholder 识别(幂等守卫)
        return (
            f"{OFFLOAD_PREFIX} call_id={call_id}, {nbytes} bytes saved to {rel_path}]\n"
            f'完整结果已落盘。用 file_read(path="{rel_path}", offset=, limit=) '
            "按行分页回读(勿一次全读)。\n"
            f"预览(head/tail):\n{preview}"
        )

    @staticmethod
    def _thread_id_for(ctx: CompressionContext, call_id: str) -> str:
        """取该 output 条目所属 thread_id(路径按 thread 分目录,便于级联清理)。"""
        for it in ctx.history:
            if it.kind == "function_call_output" and it.payload["call_id"] == call_id:
                return it.thread_id
        # 不可达:call_id 来自遍历 history 的 output 条目
        raise KeyError(call_id)
