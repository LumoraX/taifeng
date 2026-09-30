"""WorkspaceFS（ADR 0113）：文件类工具经协议读写工作区，不再只认本机目录。"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING, Any

import pytest

from taifeng import (
    CancellationToken,
    LocalWorkspaceFS,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    ToolContext,
    ToolResult,
    ToolSpec,
    WorkspaceEntry,
    WorkspaceFileInfo,
    WorkspaceFS,
    WorkspacePathError,
    make_apply_patch_tool,
    make_file_read_tool,
    make_file_write_tool,
    make_glob_tool,
    make_grep_tool,
)

if TYPE_CHECKING:
    from pathlib import Path


def _ctx(call_id: str = "c1") -> ToolContext:
    return ToolContext(call_id=call_id, cancel=CancellationToken(), thread_id="t")


async def _call(tool: ToolSpec, **args: Any) -> ToolResult:
    return await tool.handler(args, _ctx())


# ---------------------------------------------------------------------------
# 本机实现
# ---------------------------------------------------------------------------


def test_local_workspace_satisfies_the_protocol(tmp_path: Path) -> None:
    fs = LocalWorkspaceFS(tmp_path)
    assert isinstance(fs, WorkspaceFS)
    assert fs.root == str(tmp_path.resolve())


def test_resolve_confines_paths_to_the_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(tmp_path / "outside.txt")
    fs = LocalWorkspaceFS(root)

    assert fs.resolve("a/b.txt") == str(root.resolve() / "a" / "b.txt")
    assert fs.resolve("") == str(root.resolve())
    for escaping in ("../outside.txt", str(tmp_path / "outside.txt"), "a/../../outside.txt", "link"):
        with pytest.raises(WorkspacePathError):
            fs.resolve(escaping)
    # 越界是 PermissionError 的一种：调用方按标准文件系统异常处理即可
    assert issubclass(WorkspacePathError, PermissionError)


async def test_every_method_checks_the_boundary_itself(tmp_path: Path) -> None:
    """实现不假设调用方先调过 resolve。"""
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    fs = LocalWorkspaceFS(root)
    with pytest.raises(WorkspacePathError):
        await fs.read_bytes("../outside.txt")
    with pytest.raises(WorkspacePathError):
        await fs.write_bytes("../outside.txt", b"x")
    with pytest.raises(WorkspacePathError):
        await fs.metadata("../outside.txt")
    with pytest.raises(WorkspacePathError):
        await fs.list_directory("..")
    with pytest.raises(WorkspacePathError):
        await fs.remove("../outside.txt")
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "secret"


async def test_local_read_write_metadata_list_remove(tmp_path: Path) -> None:
    fs = LocalWorkspaceFS(tmp_path)
    assert await fs.metadata("a/b.txt") == WorkspaceFileInfo(exists=False)

    await fs.write_bytes("a/b.txt", "你好".encode())
    assert await fs.read_bytes("a/b.txt") == "你好".encode()
    # 规范路径与相对路径指向同一个文件
    assert await fs.read_bytes(fs.resolve("a/b.txt")) == "你好".encode()
    info = await fs.metadata("a/b.txt")
    assert (info.exists, info.is_file, info.is_directory, info.size) == (True, True, False, 6)
    assert (await fs.metadata("a")).is_directory
    # 原子写：不留临时文件
    assert sorted(p.name for p in (tmp_path / "a").iterdir()) == ["b.txt"]

    (tmp_path / "a" / "sub").mkdir()
    (tmp_path / "a" / "link").symlink_to(tmp_path / "a" / "b.txt")
    entries = sorted(await fs.list_directory("a"), key=lambda e: e.name)
    assert entries == [
        WorkspaceEntry(name="b.txt", is_directory=False, is_file=True),
        WorkspaceEntry(name="link", is_directory=False, is_file=False, is_symlink=True),
        WorkspaceEntry(name="sub", is_directory=True, is_file=False),
    ]

    await fs.remove("a/b.txt")
    assert not (tmp_path / "a" / "b.txt").exists()
    with pytest.raises(FileNotFoundError):
        await fs.remove("a/b.txt")
    with pytest.raises(FileNotFoundError):
        await fs.read_bytes("a/b.txt")


async def test_local_write_without_parents_and_directory_removal(tmp_path: Path) -> None:
    fs = LocalWorkspaceFS(tmp_path)
    with pytest.raises(FileNotFoundError):
        await fs.write_bytes("missing/x.txt", b"x", create_parents=False)
    await fs.write_bytes("d/x.txt", b"x")
    with pytest.raises(OSError):  # noqa: PT011  # 非空目录、未要求递归：具体子类随平台而异
        await fs.remove("d")
    assert (tmp_path / "d" / "x.txt").exists()
    await fs.remove("d", recursive=True)
    assert not (tmp_path / "d").exists()
    with pytest.raises(FileNotFoundError):
        await fs.list_directory("d")


# ---------------------------------------------------------------------------
# 非本机实现：文件类工具整套跑在它上面
# ---------------------------------------------------------------------------


class MemoryWorkspace:
    """内存里的工作区：代表容器 / 远端沙盒里的文件系统。"""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.calls: list[str] = []

    @property
    def root(self) -> str:
        return "sandbox:/work"

    def resolve(self, path: str) -> str:
        """规范路径 = 根 + "/" + 相对路径；``..`` 走出根、别处的绝对路径都算越界。"""
        if path.startswith(self.root):
            path = path[len(self.root):]
        elif path.startswith("/"):
            raise WorkspacePathError(path)
        parts: list[str] = []
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise WorkspacePathError(path)
                parts.pop()
            else:
                parts.append(part)
        return "/".join([self.root, *parts])

    def _key(self, path: str) -> str:
        return self.resolve(path)[len(self.root):].lstrip("/")

    def _is_dir(self, key: str) -> bool:
        return key == "" or any(name.startswith(key + "/") for name in self.files)

    async def read_bytes(self, path: str) -> bytes:
        key = self._key(path)
        self.calls.append(f"read:{key}")
        if key not in self.files:
            raise FileNotFoundError(path)
        return self.files[key]

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        key = self._key(path)
        self.calls.append(f"write:{key}")
        parent = posixpath.dirname(key)
        if not create_parents and parent and not self._is_dir(parent):
            raise FileNotFoundError(parent)
        self.files[key] = data

    async def metadata(self, path: str) -> WorkspaceFileInfo:
        key = self._key(path)
        if key in self.files:
            return WorkspaceFileInfo(exists=True, is_file=True, size=len(self.files[key]))
        if self._is_dir(key):
            return WorkspaceFileInfo(exists=True, is_directory=True)
        return WorkspaceFileInfo(exists=False)

    async def list_directory(self, path: str) -> list[WorkspaceEntry]:
        key = self._key(path)
        self.calls.append(f"list:{key}")
        if not self._is_dir(key):
            raise FileNotFoundError(path)
        prefix = key + "/" if key else ""
        names: dict[str, bool] = {}
        for name in self.files:
            if name.startswith(prefix):
                head, _, rest = name[len(prefix):].partition("/")
                names[head] = names.get(head, False) or bool(rest)
        return [
            WorkspaceEntry(name=name, is_directory=is_dir, is_file=not is_dir)
            for name, is_dir in names.items()
        ]

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        key = self._key(path)
        self.calls.append(f"remove:{key}")
        if key in self.files:
            del self.files[key]
            return
        if not self._is_dir(key):
            raise FileNotFoundError(path)
        if not recursive:
            raise OSError(f"directory not empty: {path}")
        for name in [n for n in self.files if n.startswith(key + "/")]:
            del self.files[name]


def test_memory_workspace_satisfies_the_protocol() -> None:
    assert isinstance(MemoryWorkspace(), WorkspaceFS)


async def test_file_read_goes_through_the_workspace(tmp_path: Path) -> None:
    fs = MemoryWorkspace({"notes/a.txt": "line1\nline2\nline3".encode()})
    tool = make_file_read_tool(workspace=fs)
    assert "sandbox:/work" in tool.description

    whole = await _call(tool, path="notes/a.txt")
    assert not whole.is_error and whole.output == "line1\nline2\nline3"
    assert whole.data["path"] == "sandbox:/work/notes/a.txt"
    paged = await _call(tool, path="notes/a.txt", offset=1, limit=1)
    assert paged.output == "line2"

    missing = await _call(tool, path="notes/none.txt")
    assert missing.is_error and missing.data.get("reason") == "not_found"
    folder = await _call(tool, path="notes")
    assert folder.is_error and folder.data.get("reason") == "not_found"
    outside = await _call(tool, path="../etc/passwd")
    assert outside.is_error and outside.data.get("reason") == "sandbox_violation"
    # 工作区在别处：本机目录没有被碰过
    assert list(tmp_path.iterdir()) == []


async def test_file_write_goes_through_the_workspace() -> None:
    fs = MemoryWorkspace()
    tool = make_file_write_tool(workspace=fs)
    result = await _call(tool, path="out/report.md", content="# 报告")
    assert not result.is_error and result.data["path"] == "sandbox:/work/out/report.md"
    assert fs.files == {"out/report.md": "# 报告".encode()}

    outside = await _call(tool, path="/etc/passwd", content="x")
    assert outside.is_error and outside.data.get("reason") == "sandbox_violation"
    too_big = await _call(make_file_write_tool(workspace=fs, max_bytes=3), path="x", content="abcd")
    assert too_big.is_error and too_big.data.get("reason") == "too_large"
    assert list(fs.files) == ["out/report.md"]


async def test_write_failure_is_reported_not_raised() -> None:
    class _Broken(MemoryWorkspace):
        async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
            raise OSError("disk full")

    result = await _call(make_file_write_tool(workspace=_Broken()), path="x.txt", content="x")
    assert result.is_error and result.data.get("reason") == "write_error" and "disk full" in result.output


async def test_apply_patch_goes_through_the_workspace() -> None:
    fs = MemoryWorkspace({"a.py": b"x = 1\n", "old.txt": b"bye"})
    tool = make_apply_patch_tool(workspace=fs)
    result = await _call(tool, patches=[
        {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
        {"path": "new/b.py", "new_text": "y = 1\n", "create": True},
        {"path": "old.txt", "delete": True},
    ])
    assert not result.is_error, result.output
    assert fs.files == {"a.py": b"x = 2\n", "new/b.py": b"y = 1\n"}
    assert [a["path"] for a in result.data["applied"]] == [
        "sandbox:/work/a.py", "sandbox:/work/new/b.py", "sandbox:/work/old.txt",
    ]


async def test_apply_patch_validates_everything_before_writing_anything() -> None:
    fs = MemoryWorkspace({"a.py": b"x = 1\n", "dir/inner.txt": b"i"})
    tool = make_apply_patch_tool(workspace=fs)
    for bad in (
        {"path": "missing.py", "old_text": "a", "new_text": "b"},
        {"path": "a.py", "new_text": "dup", "create": True},
        {"path": "../escape.py", "new_text": "x", "create": True},
        # 目录不能当文件删：在校验阶段就拒绝，不留到应用阶段半途失败
        {"path": "dir", "delete": True},
    ):
        result = await _call(tool, patches=[
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, bad,
        ])
        assert result.is_error and result.data.get("reason") == "patch_validation_failed"
        assert fs.files == {"a.py": b"x = 1\n", "dir/inner.txt": b"i"}
    assert not any(call.startswith(("write:", "remove:")) for call in fs.calls)


class _Recorder:
    """记录审批请求并按 target 决定放行。"""

    def __init__(self, deny: str | None = None) -> None:
        self.requests: list[PermissionRequest] = []
        self._deny = deny

    async def prompt(self, request: PermissionRequest) -> PermissionDecision:
        self.requests.append(request)
        if self._deny is not None and self._deny in request.target:
            return PermissionDecision.deny(reason="no")
        return PermissionDecision.allow(reason="ok")


async def test_permission_targets_are_workspace_paths() -> None:
    """审批看到的是工作区里的规范路径；被拒的请求不触碰工作区。"""
    fs = MemoryWorkspace({"a.txt": b"A", "secret/key": b"K"})
    recorder = _Recorder(deny="secret")
    policy = PermissionPolicy(default_mode="ask", prompter=recorder)

    assert not (await _call(make_file_read_tool(workspace=fs, policy=policy), path="a.txt")).is_error
    denied = await _call(make_file_read_tool(workspace=fs, policy=policy), path="secret/key")
    assert denied.is_error and denied.data.get("reason") == "permission_denied"
    denied_patch = await _call(make_apply_patch_tool(workspace=fs, policy=policy), patches=[
        {"path": "a.txt", "old_text": "A", "new_text": "B"},
        {"path": "secret/key", "delete": True},
    ])
    assert denied_patch.is_error and denied_patch.data.get("reason") == "permission_denied"

    assert [(r.scope, r.target) for r in recorder.requests] == [
        ("file_read", "sandbox:/work/a.txt"),
        ("file_read", "sandbox:/work/secret/key"),
        ("file_write", "sandbox:/work/a.txt"),
        ("file_write", "sandbox:/work/secret/key"),
    ]
    assert fs.files == {"a.txt": b"A", "secret/key": b"K"}
    assert fs.calls == ["read:a.txt"]


@pytest.mark.parametrize(
    "factory", [make_file_read_tool, make_file_write_tool, make_apply_patch_tool,
                make_glob_tool, make_grep_tool],
)
def test_root_dir_and_workspace_are_mutually_exclusive(factory: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="root_dir / workspace"):
        factory(root_dir=tmp_path, workspace=MemoryWorkspace())
    with pytest.raises(ValueError, match="root_dir / workspace"):
        factory()
    with pytest.raises(TypeError, match="WorkspaceFS"):
        factory(workspace=object())


async def test_root_dir_still_means_the_local_directory(tmp_path: Path) -> None:
    """既有写法不变：``root_dir`` 就是本机工作区。"""
    await _call(make_file_write_tool(root_dir=tmp_path), path="a/b.txt", content="hi")
    assert (tmp_path / "a" / "b.txt").read_text(encoding="utf-8") == "hi"
    same = await _call(make_file_read_tool(workspace=LocalWorkspaceFS(tmp_path)), path="a/b.txt")
    assert same.output == "hi" and same.data["path"] == str(tmp_path.resolve() / "a" / "b.txt")


# ---------------------------------------------------------------------------
# 搜索工具：同一套遍历逻辑跑在非本机工作区上
# ---------------------------------------------------------------------------


def _tree() -> MemoryWorkspace:
    return MemoryWorkspace({
        "src/app.py": b"import os\nTOKEN = 'x'\n",
        "src/util/helpers.py": b"def helper():\n    return TOKEN\n",
        "docs/readme.md": b"# readme\nTOKEN appears here\n",
        "node_modules/pkg/index.js": b"TOKEN\n",
        ".gitignore": b"build/\n",
        "build/out.py": b"TOKEN = 'generated'\n",
        "data.bin": b"\x00\x01TOKEN",
    })


async def test_glob_walks_a_non_local_workspace() -> None:
    fs = _tree()
    tool = make_glob_tool(workspace=fs)
    assert "sandbox:/work" in tool.description

    result = await _call(tool, pattern="**/*.py")
    assert not result.is_error, result.output
    lines = result.output.splitlines()
    assert lines[:2] == ["src/app.py", "src/util/helpers.py"]
    # 排除目录不下探、.gitignore 生效并在尾注告知
    assert "node_modules" not in result.output and "build/out.py" not in result.output
    assert "ignored by .gitignore" in result.output
    assert not any(call.startswith("list:node_modules") for call in fs.calls)

    scoped = await _call(tool, pattern="*.py", path="src/util")
    assert scoped.output.splitlines()[0] == "src/util/helpers.py"
    missing = await _call(tool, pattern="*.py", path="nope")
    assert missing.is_error and missing.data.get("reason") == "not_found"
    outside = await _call(tool, pattern="*.py", path="../x")
    assert outside.is_error and outside.data.get("reason") == "sandbox_violation"


async def test_grep_scans_a_non_local_workspace() -> None:
    fs = _tree()
    tool = make_grep_tool(workspace=fs)
    result = await _call(tool, pattern="TOKEN", output_mode="content")
    assert not result.is_error, result.output
    assert "docs/readme.md:2:TOKEN appears here" in result.output
    assert "src/app.py:2:TOKEN = 'x'" in result.output
    assert "src/util/helpers.py:2:    return TOKEN" in result.output
    # 二进制文件跳过并告知；被忽略与被排除的目录不搜
    assert "binary" in result.output
    assert "build/out.py" not in result.output and "node_modules" not in result.output

    single = await _call(tool, pattern="helper", path="src/util/helpers.py", output_mode="count")
    assert single.output.splitlines()[0] == "src/util/helpers.py:1"
    ignored_on_purpose = await _call(tool, pattern="generated", path="build")
    assert "build/out.py" in ignored_on_purpose.output


async def test_search_in_a_non_local_workspace_honours_cancellation() -> None:
    fs = _tree()
    token = CancellationToken()
    token.cancel()
    result = await make_grep_tool(workspace=fs).handler(
        {"pattern": "TOKEN"}, ToolContext(call_id="c", cancel=token, thread_id="t"))
    assert result.is_error and result.data.get("reason") == "cancelled"
    assert fs.calls == []


async def test_oversized_files_are_skipped_without_being_read() -> None:
    fs = MemoryWorkspace({"big.log": b"TOKEN\n" * 1000, "small.txt": b"TOKEN\n"})
    result = await _call(make_grep_tool(workspace=fs, max_file_bytes=100), pattern="TOKEN")
    assert "small.txt" in result.output
    assert "larger than 100 bytes: big.log" in result.output
    assert "read:big.log" not in fs.calls


async def test_symlinks_in_a_non_local_workspace_are_skipped_and_counted() -> None:
    """协议不给链接目标，无法判定是否仍在工作区内：一律不跟随，并在尾注告知。"""

    class _WithLink(MemoryWorkspace):
        async def list_directory(self, path: str) -> list[WorkspaceEntry]:
            entries = await super().list_directory(path)
            if self._key(path) == "":
                entries.append(WorkspaceEntry(
                    name="link.py", is_directory=False, is_file=False, is_symlink=True))
            return entries

    result = await _call(make_glob_tool(workspace=_WithLink({"a.py": b"x"})), pattern="*.py")
    assert result.output.splitlines()[0] == "a.py"
    assert "link.py" not in result.output.splitlines()
    assert "skipped 1 symlink(s)" in result.output


async def test_a_failing_workspace_is_reported_as_unreadable_not_raised() -> None:
    class _Flaky(MemoryWorkspace):
        async def list_directory(self, path: str) -> list[WorkspaceEntry]:
            if self._key(path) == "broken":
                raise OSError("connection reset")
            return await super().list_directory(path)

    fs = _Flaky({"ok/a.py": b"x", "broken/b.py": b"x"})
    result = await _call(make_glob_tool(workspace=fs), pattern="**/*.py")
    assert not result.is_error
    assert result.output.splitlines()[0] == "ok/a.py"
    assert "skipped 1 unreadable path(s)" in result.output
