"""offload 的存储一致性：非本机写读、失败、取消、路径边界与冷恢复。"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import TYPE_CHECKING

import anyio
import pytest

from taifeng.context.injection import InitialContextInjection
from taifeng.context.strategies import offload as offload_module
from taifeng.context.strategies.offload import OffloadStrategy
from taifeng.conversation import JsonlMessageStore
from taifeng.loop.cancellation import CancellationToken
from taifeng.tool.builtins import make_file_read_tool
from taifeng.tool.spec import ToolContext
from taifeng.tool.workspace import LocalWorkspaceFS, WorkspacePathError
from tests.context.test_offload_strategy import BIG, TID, _ctx, _history, _pair
from tests.tool.test_workspace_fs import MemoryWorkspace

if TYPE_CHECKING:
    from pathlib import Path

INJECTION = InitialContextInjection.DO_NOT_INJECT
REL_PATH = f"_offload/{TID}/c1"


async def test_remote_offload_and_paged_reader_share_workspace(tmp_path: Path) -> None:
    """远端字节完整保存，模型按 stub 路径分页回读，本机不落文件。"""
    workspace = MemoryWorkspace()
    body = "\r\n".join(f"第{i}行：你好 🌍" for i in range(2000))
    history = _history(body)
    result = await OffloadStrategy(workspace=workspace).compress(_ctx(history), INJECTION)
    assert result.success
    assert workspace.files[REL_PATH] == body.encode("utf-8")
    assert list(tmp_path.iterdir()) == []
    assert result.new_history[2].id == history[2].id
    assert result.new_history[2].thread_id == history[2].thread_id
    assert REL_PATH in result.new_history[2].payload["output"]
    assert history[2].payload["output"] == body
    assert not result.cache_invalidated and result.anchor_preserved_until == 0
    assert result.detail == {"offloaded": 1, "bytes_saved": len(body.encode("utf-8"))}
    reader = make_file_read_tool(workspace=workspace, max_bytes=1024)
    recalled = await reader.handler(
        {"path": REL_PATH, "offset": 1500, "limit": 2},
        ToolContext(call_id="read", cancel=CancellationToken(), thread_id=TID),
    )
    assert not recalled.is_error
    assert recalled.output == "\n".join(body.splitlines()[1500:1502])


def test_offload_storage_arguments_are_exclusive(tmp_path: Path) -> None:
    """装配错误在构造时明确报错，沿用文件工具的二选一约定。"""
    with pytest.raises(ValueError):
        OffloadStrategy()
    with pytest.raises(ValueError):
        OffloadStrategy(file_root=tmp_path, workspace=MemoryWorkspace())
    with pytest.raises(TypeError, match="WorkspaceFS"):
        OffloadStrategy(workspace=object())  # type: ignore[arg-type]


@pytest.mark.parametrize("body", ["", "small"])
async def test_small_results_do_not_touch_workspace(body: str) -> None:
    """空输出与小结果不访问工作区。"""
    workspace = MemoryWorkspace()
    strategy = OffloadStrategy(workspace=workspace)
    result = await strategy.compress(_ctx(_history(body)), INJECTION)
    assert not result.success
    assert workspace.calls == []


class FailingWorkspace(MemoryWorkspace):
    """第一条写入失败，其余正常，验证逐条失败隔离。"""

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        """首条不写入，明确返回标准 IO 失败。"""
        if path.endswith("/c1"):
            raise OSError("backend unavailable")
        await super().write_bytes(path, data, create_parents=create_parents)


async def test_write_failure_keeps_original_and_continues(caplog: pytest.LogCaptureFixture) -> None:
    """失败条目原文不变、无 stub，下一条仍可成功且有日志。"""
    workspace = FailingWorkspace()
    history = [*_pair("c1", "search", BIG), *_pair("c2", "search", BIG)]
    result = await OffloadStrategy(workspace=workspace).compress(_ctx(history, anchor=-1), INJECTION)
    assert result.success and result.detail["offloaded"] == 1
    assert result.new_history[1].payload["output"] == BIG
    assert "[offloaded:" in result.new_history[3].payload["output"]
    assert REL_PATH not in workspace.files
    assert "backend unavailable" in caplog.text


class WaitingWorkspace(MemoryWorkspace):
    """写入挂起直到取消，不在存储里发布任何内容。"""

    def __init__(self) -> None:
        """初始化可观测的写入开始信号。"""
        super().__init__()
        self.started = anyio.Event()

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        """等待取消，由调用者检验传播和历史不可变。"""
        self.started.set()
        await anyio.sleep_forever()


async def test_cancel_while_backend_is_waiting_preserves_history() -> None:
    """后端等待响应取消，历史原文保持完整。"""
    workspace = WaitingWorkspace()
    history = _history()
    finished = False

    async def compress() -> None:
        """取消不应被转换成正常返回。"""
        nonlocal finished
        await OffloadStrategy(workspace=workspace).compress(_ctx(history), INJECTION)
        finished = True

    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(compress)
            await workspace.started.wait()
            group.cancel_scope.cancel()
    assert not finished
    assert not workspace.files
    assert history[2].payload["output"] == BIG


@pytest.mark.parametrize("bad", ["", ".", "..", "../escape", "a/b", "a\\b", "a\x00b", "/abs"])
async def test_invalid_ids_do_not_touch_backend(bad: str) -> None:
    """写入和清理均先验证外部路径段，不让后端接到危险路径。"""
    workspace = MemoryWorkspace()
    strategy = OffloadStrategy(workspace=workspace)
    result = await strategy.compress(_ctx(_history(call_id=bad)), INJECTION)
    assert not result.success
    await strategy.cleanup_thread(bad)
    assert workspace.calls == []


async def test_workspace_rejects_escape_without_publishing_stub(tmp_path: Path) -> None:
    """本机 _offload 目录链接到根外时，协议拦截写入。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "_offload").symlink_to(outside, target_is_directory=True)
    result = await OffloadStrategy(workspace=LocalWorkspaceFS(root)).compress(_ctx(_history()), INJECTION)
    assert not result.success
    assert list(outside.iterdir()) == []


async def test_cleanup_uses_backend_and_only_removes_target_thread() -> None:
    """清理经同一后端，缺失幂等，不删除其他 thread 的数据。"""
    workspace = MemoryWorkspace({REL_PATH: b"a", "_offload/other/c1": b"b"})
    strategy = OffloadStrategy(workspace=workspace)
    await strategy.cleanup_thread(TID)
    await strategy.cleanup_thread(TID)
    assert workspace.files == {"_offload/other/c1": b"b"}
    assert workspace.calls == [f"remove:_offload/{TID}"] * 2


class DeniedCleanupWorkspace(MemoryWorkspace):
    """非缺失的清理错误必须让宿主知道。"""

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """模拟远端拒绝删除。"""
        raise PermissionError("cleanup denied")


async def test_cleanup_errors_are_not_silently_ignored() -> None:
    """清理失败不伪装成功。"""
    with pytest.raises(PermissionError, match="cleanup denied"):
        await OffloadStrategy(workspace=DeniedCleanupWorkspace()).cleanup_thread(TID)


async def test_concurrent_distinct_calls_have_independent_files() -> None:
    """并发 offload 不同 call_id 不串写，各条可独立回读。"""
    workspace = MemoryWorkspace()
    strategy = OffloadStrategy(workspace=workspace)

    async def offload(call_id: str, body: str) -> None:
        """压缩一条独立调用。"""
        result = await strategy.compress(_ctx(_history(body, call_id=call_id)), INJECTION)
        assert result.success and result.detail["offloaded"] == 1

    async with anyio.create_task_group() as group:
        group.start_soon(offload, "c1", BIG)
        group.start_soon(offload, "c2", BIG + "tail")
    assert workspace.files[REL_PATH] == BIG.encode()
    assert workspace.files[f"_offload/{TID}/c2"] == (BIG + "tail").encode()


async def test_jsonl_resume_reuses_persisted_remote_bytes(tmp_path: Path) -> None:
    """重建 store、策略与工作区客户端后，stub 幂等且回读原始字节。"""
    workspace = MemoryWorkspace()
    result = await OffloadStrategy(workspace=workspace).compress(_ctx(_history()), INJECTION)
    store_path = tmp_path / "store"
    store = JsonlMessageStore(store_path)
    for item in result.new_history:
        await store.append(item)
    resumed = JsonlMessageStore(store_path)
    history = [item async for item in await resumed.load_thread(TID)]
    reconnected = MemoryWorkspace(workspace.files)
    again = await OffloadStrategy(workspace=reconnected).compress(_ctx(history), INJECTION)
    assert not again.success and reconnected.calls == []
    reader = make_file_read_tool(workspace=reconnected)
    recalled = await reader.handler(
        {"path": REL_PATH}, ToolContext(call_id="r", cancel=CancellationToken(), thread_id=TID),
    )
    assert not recalled.is_error and recalled.output == BIG


class EscapingWorkspace(MemoryWorkspace):
    """模拟后端明确拒绝解析目标路径。"""

    def resolve(self, path: str) -> str:
        """拒绝所有 offload 路径。"""
        raise WorkspacePathError("outside backend root")


async def test_resolve_errors_preserve_original_output() -> None:
    """路径解析失败也属于可恢复的 IO 失败，不外抛。"""
    result = await OffloadStrategy(workspace=EscapingWorkspace()).compress(_ctx(_history()), INJECTION)
    assert not result.success


class WindowsWorkspace(LocalWorkspaceFS):
    """模拟 Windows 默认后端的规范路径，不依赖测试宿主平台。"""

    def __init__(self, root: Path) -> None:
        """保留本机后端身份，只模拟平台路径与写入。"""
        super().__init__(root)
        self.written: dict[str, bytes] = {}

    def resolve(self, path: str) -> str:
        """Windows 的本机规范路径含反斜杠。"""
        return str(PureWindowsPath("C:/workspace") / path)

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        """记录成功提交的内容，不在本机伪造 Windows 目录。"""
        self.written[path] = data


async def test_local_windows_paths_preserve_file_root_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机父目录比较按当前平台语义处理，反斜杠不误判越界。"""
    workspace = WindowsWorkspace(tmp_path)
    monkeypatch.setattr(offload_module, "Path", PureWindowsPath)
    result = await OffloadStrategy(workspace=workspace).compress(_ctx(_history()), INJECTION)
    assert result.success
    assert workspace.written == {REL_PATH: BIG.encode()}


async def test_cleanup_final_symlink_only_removes_the_link(tmp_path: Path) -> None:
    """清理链接条目不能先解析成目标目录，否则会删其他 thread。"""
    other = tmp_path / "_offload" / "other"
    other.mkdir(parents=True)
    (other / "c1").write_text("keep", encoding="utf-8")
    link = tmp_path / "_offload" / TID
    link.symlink_to(other, target_is_directory=True)
    await OffloadStrategy(file_root=tmp_path).cleanup_thread(TID)
    assert not link.is_symlink()
    assert (other / "c1").read_text(encoding="utf-8") == "keep"
