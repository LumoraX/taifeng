"""工作区 offload 的真实能力场景：大结果先变 stub，再由同一工作区分页回读。"""

from __future__ import annotations

import json
import posixpath
import re
import secrets
from typing import TYPE_CHECKING, Any

import anyio
from _drivers_extra import _blob, _run_turn, _tool_pairs
from _setups import ScenarioSetup

from taifeng import ToolResult, ToolSpec
from taifeng.context.placeholders import OFFLOAD_PREFIX
from taifeng.context.strategies.offload import OffloadStrategy
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.tool.builtins import make_file_read_tool
from taifeng.tool.workspace import WorkspaceEntry, WorkspaceFileInfo, WorkspacePathError

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.llm.types import ApiRequest

FETCH_TOOL = "fetch_large_record"
MIDDLE_LINE = 450
_TOTAL_LINES = 900


class ExampleWorkspaceFS:
    """仅供场景使用的内存工作区；规范路径刻意使用非本机 URI，不建立宿主文件。"""

    def __init__(self) -> None:
        """初始化文件、目录与实际读写记录。"""
        self.files: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.writes: list[str] = []
        self._directories = {self.root}

    @property
    def root(self) -> str:
        """返回给工具描述和权限检查使用的工作区标识。"""
        return "memory://offload-validation"

    def resolve(self, path: str) -> str:
        """接收相对路径或本工作区 URI，拒绝越界和其他工作区。"""
        relative = path.removeprefix(self.root + "/")
        if path == self.root:
            return self.root
        if relative.startswith("/") or "://" in relative or "\x00" in relative:
            raise WorkspacePathError(path)
        normalized = posixpath.normpath(relative)
        if normalized == ".." or normalized.startswith("../"):
            raise WorkspacePathError(path)
        return self.root if normalized == "." else f"{self.root}/{normalized}"

    async def read_bytes(self, path: str) -> bytes:
        """从工作区读取文件，同时记录实际访问的规范路径。"""
        await anyio.lowlevel.checkpoint()
        resolved = self.resolve(path)
        if resolved not in self.files:
            raise FileNotFoundError(path)
        self.reads.append(resolved)
        return self.files[resolved]

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        """原子替换内存文件；按参数创建父目录。"""
        await anyio.lowlevel.checkpoint()
        resolved = self.resolve(path)
        parent = resolved.rsplit("/", 1)[0]
        if not create_parents and parent not in self._directories:
            raise FileNotFoundError(parent)
        if resolved in self._directories:
            raise IsADirectoryError(path)
        while parent != self.root:
            self._directories.add(parent)
            parent = parent.rsplit("/", 1)[0]
        self.files[resolved] = bytes(data)
        self.writes.append(resolved)

    async def metadata(self, path: str) -> WorkspaceFileInfo:
        """返回内存文件或目录的元数据，不存在时返回空记录。"""
        await anyio.lowlevel.checkpoint()
        resolved = self.resolve(path)
        if resolved in self.files:
            return WorkspaceFileInfo(exists=True, is_file=True, size=len(self.files[resolved]))
        return WorkspaceFileInfo(
            exists=resolved in self._directories, is_directory=resolved in self._directories,
        )

    async def list_directory(self, path: str) -> list[WorkspaceEntry]:
        """列出目录的直接子项，不递归。"""
        await anyio.lowlevel.checkpoint()
        resolved = self.resolve(path)
        if resolved not in self._directories:
            raise FileNotFoundError(path)
        prefix = resolved + "/"
        return [
            WorkspaceEntry(
                name=entry.removeprefix(prefix),
                is_directory=entry in self._directories, is_file=entry in self.files,
            )
            for entry in sorted(self._directories | self.files.keys())
            if entry.startswith(prefix) and "/" not in entry.removeprefix(prefix)
        ]

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """删除内存文件或目录；非空目录须显式要求递归。"""
        await anyio.lowlevel.checkpoint()
        resolved = self.resolve(path)
        children = [entry for entry in self._directories | self.files.keys()
                    if entry.startswith(resolved + "/")]
        if resolved not in self.files and resolved not in self._directories:
            raise FileNotFoundError(path)
        if children and not recursive:
            raise OSError("directory is not empty")
        for entry in [resolved, *children]:
            self.files.pop(entry, None)
            self._directories.discard(entry)


def _large_record(token: str) -> str:
    """在 head/tail 预览之外的第 450 行埋随机口令，首行给出零基行号。"""
    lines = [f"row={index:04d} " + "sample observation remains stable; " * 3
             for index in range(_TOTAL_LINES)]
    lines[0] = f"MIDDLE-TOKEN 位于零基行号 {MIDDLE_LINE}；请按行分页回读。"
    lines[MIDDLE_LINE] = f"MIDDLE-TOKEN: {token}"
    return "\n".join(lines)


def setup_offload_workspace(root: Path, client: Any) -> ScenarioSetup:
    """以同一个非本机工作区装配 offload 与 file_read，并开启实际请求留痕。"""
    workspace = ExampleWorkspaceFS()
    token = "OFFLOAD-" + secrets.token_hex(12).upper()

    async def fetch(args: dict[str, Any], ctx: Any) -> ToolResult:
        """只读返回本次装配生成的超长结果，口令不放进任何模型预先可见的描述。"""
        return ToolResult.ok(_large_record(token))

    tool = ToolSpec(
        name=FETCH_TOOL, description="取得完整观测记录；可能很长，首行说明待查口令的行号。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=fetch, parallel_safe=True,
    )
    return ScenarioSetup(
        extra_tools=[tool, make_file_read_tool(workspace=workspace, max_bytes=2048)],
        compressors=[OffloadStrategy(workspace=workspace)],
        pool_kwargs={"enable_request_capture": True},
        budget_kwargs={"soft_limit_ratio": 0.5, "output_reserve_tokens": 1024},
        state={"workspace": workspace, "token": token},
    )


def _request_output(request: dict[str, Any], call_id: str) -> str:
    """从请求的兼容 messages 视图取指定工具结果。"""
    return next((str(message.get("content", "")) for message in request.get("messages", [])
                 if message.get("role") == "tool" and message.get("tool_call_id") == call_id), "")


def _verify_stub_requests(res: Any, call_id: str, path: str, token: str) -> None:
    """证明工具后的首次实际请求已是 stub，且回读成功之前没有任何请求见过口令。"""
    completed = next(index for index, event in enumerate(res.events)
                     if event.kind == "tool_call_completed" and event.data["call_id"] == call_id)
    requests = [(index, event.data) for index, event in enumerate(res.events)
                if event.kind == "llm_request_recorded" and index > completed]
    assert requests, "没有捕获到大工具结果之后的模型请求"
    first_index, first_request = requests[0]
    first_output = _request_output(first_request, call_id)
    assert first_output.startswith(OFFLOAD_PREFIX) and path in first_output, "首次工具后请求不是 stub"
    assert token not in _blob(first_request), "首次 stub 请求泄露中段口令"
    assert any(event.kind == "compaction_completed" and event.data.get("success")
               and event.data.get("detail", {}).get("offloaded", 0) > 0
               for event in res.events[completed:first_index]), "采样前未完成有效 offload"
    successful_reads = [index for index, event in enumerate(res.events)
                        if event.kind == "tool_call_completed" and not event.data.get("is_error")
                        and event.data["call_id"] != call_id
                        and token in str(event.data.get("output", ""))]
    assert successful_reads, "没有工具回传中段口令"
    for event in res.events[:successful_reads[0]]:
        if event.kind == "llm_request_recorded":
            assert token not in _blob(event.data), "分页回读前模型已见过完整口令"


def _verify_workspace_read(res: Any, path: str, token: str) -> None:
    """核对工具分页参数、成功输出与同一工作区的实际读写记录。"""
    workspace = res.state["workspace"]
    canonical = workspace.resolve(path)
    assert canonical in workspace.writes and canonical in workspace.reads, "工作区未实际写入并回读"
    for started, completed in _tool_pairs(res, "file_read"):
        args = json.loads(started.data["arguments"])
        if workspace.resolve(args.get("path", "")) != canonical:
            continue
        offset, limit = args.get("offset"), args.get("limit")
        if (type(offset) is not int or type(limit) is not int or offset <= 0
                or not 0 < limit < _TOTAL_LINES or not offset <= MIDDLE_LINE < offset + limit):
            continue
        if completed is not None and not completed.data["is_error"]:
            output = completed.data["output"]
            if token in output and len(output.encode("utf-8")) < 8 * 1024:
                return
    raise AssertionError("file_read 未通过 stub 路径分页成功回传中段口令")


def verify_offload_evidence(res: Any, thread_id: str, answer: str) -> dict[str, Any]:
    """完整证据必须同时成立，最终答案碰巧正确不能代替工作区 offload 生效。"""
    token = res.state["token"]
    pairs = _tool_pairs(res, FETCH_TOOL)
    assert pairs and pairs[0][1] is not None, "模型未成功调用大结果工具"
    started, completed = pairs[0]
    assert completed is not None and not completed.data["is_error"], "大结果工具失败"
    path = f"_offload/{thread_id}/{started.data['call_id']}"
    workspace = res.state["workspace"]
    original = workspace.files.get(workspace.resolve(path), b"")
    assert len(original) > 8 * 1024 and token.encode() in original, "工作区原文不够大或缺口令"
    _verify_stub_requests(res, started.data["call_id"], path, token)
    _verify_workspace_read(res, path, token)
    assert token in answer, "最终答案未复述中段口令"
    return {"thread_id": thread_id, "path": path, "answer": answer}


async def drive_offload_workspace(engine: Any, res: Any) -> None:
    """让模型先取大结果再分页回读，只接受四段证据全部成立的运行。"""
    answer = await _run_turn(
        engine, res, "请先取回完整观测记录，再找出其中 MIDDLE-TOKEN 的值并原样回答。"
        "如果工具结果是 offload stub，请按预览给出的零基行号，用 stub 路径调用 "
        "file_read(path, offset, limit) 读取一小页；不要猜口令，也不要一次读完整文件。", 1,
    )
    res.state["evidence"] = verify_offload_evidence(res, engine.thread_id, answer)
    print(f"  [offload] 首次后续请求已为 stub；工作区分页回读 {res.state['evidence']['path']}")


class OffloadWorkspaceSimClient(SimClient):
    """动态 conformance 剧本：从实际请求取得运行期 stub 路径与回读结果。"""

    def __init__(self) -> None:
        """使用随机 call_id，防止路径被静态模拟脚本硬编码。"""
        super().__init__(turns=[])
        self._fetch_id = "fetch-" + secrets.token_hex(4)

    def _next_turn(self, request: ApiRequest) -> SimTurn:
        """只从模型已经可见的工具结果获取路径或口令，复用 SimClient 的协议检查。"""
        outputs = [str(message.content) for message in request.messages if message.role == "tool"]
        if not outputs:
            return SimTurn(tool_calls=[{
                "id": self._fetch_id, "name": FETCH_TOOL, "arguments": "{}",
            }])
        token = re.search(r"MIDDLE-TOKEN: (OFFLOAD-[A-F0-9]+)", outputs[-1])
        if token is not None:
            return SimTurn(text=f"MIDDLE-TOKEN 是 {token.group(1)}。")
        stub = outputs[-1]
        path = re.search(r"saved to (_offload/[^\s\]]+)", stub)
        assert stub.startswith(OFFLOAD_PREFIX) and path is not None, "动态 sim 未收到 offload stub"
        return SimTurn(tool_calls=[{
            "id": "read-" + secrets.token_hex(4), "name": "file_read",
            "arguments": json.dumps({"path": path.group(1), "offset": MIDDLE_LINE, "limit": 3}),
        }])


DRIVER = {"offload_workspace": drive_offload_workspace}
