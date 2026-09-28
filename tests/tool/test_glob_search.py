"""glob 内置工具测试（ADR 0064）：正常路径 / 截断 / 沙盒与符号链接 / 权限 / 取消 / schema。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import PermissionDecision, PermissionRequest
from taifeng.tool.arg_validation import check_tool_arguments
from taifeng.tool.builtins import DEFAULT_SEARCH_EXCLUDE_DIRS, make_glob_tool
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


def _ctx(token: CancellationToken | None = None) -> ToolContext:
    """最小工具上下文。"""
    return ToolContext(call_id="c1", cancel=token or CancellationToken(), thread_id="t1")


def _touch(root: Path, *rels: str) -> None:
    """在 root 下创建若干文件（自动建父目录）。"""
    for rel in rels:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


class _RecordingPolicy:
    """记录收到的 PermissionRequest，按构造参数统一放行 / 拒绝。"""

    def __init__(self, *, grant: bool) -> None:
        self.grant = grant
        self.requests: list[PermissionRequest] = []

    async def check(self, request: PermissionRequest) -> PermissionDecision:
        self.requests.append(request)
        if self.grant:
            return PermissionDecision.allow(reason="test")
        return PermissionDecision.deny(reason="not_allowed")


async def test_glob_recursive_sorted_paths_relative_to_root(tmp_path: Path) -> None:
    """**/*.py 递归命中，按路径排序，路径相对沙盒根。"""
    _touch(tmp_path, "b.py", "a/z.py", "a/readme.md", "c.txt")
    r = await make_glob_tool(root_dir=tmp_path).handler({"pattern": "**/*.py"}, _ctx())
    assert not r.is_error
    assert r.output.splitlines() == ["a/z.py", "b.py"]
    assert r.data["count"] == 2
    assert r.data["truncated"] is False


async def test_glob_non_recursive_and_sub_path(tmp_path: Path) -> None:
    """*.py 只看基点这一层；path 指定子目录时输出仍相对沙盒根。"""
    _touch(tmp_path, "top.py", "pkg/inner.py", "pkg/deep/x.py")
    tool = make_glob_tool(root_dir=tmp_path)
    top = await tool.handler({"pattern": "*.py"}, _ctx())
    assert top.output.splitlines() == ["top.py"]
    sub = await tool.handler({"pattern": "*.py", "path": "pkg"}, _ctx())
    assert sub.output.splitlines() == ["pkg/inner.py"]


async def test_glob_no_match_is_ok(tmp_path: Path) -> None:
    """零命中是正常结果（非 error），明确告知。"""
    r = await make_glob_tool(root_dir=tmp_path).handler({"pattern": "*.rs"}, _ctx())
    assert not r.is_error
    assert r.output == "no files matched"


async def test_glob_truncation_is_announced(tmp_path: Path) -> None:
    """超过 max_results 截断，尾注明确告知且 data.truncated=True。"""
    _touch(tmp_path, *(f"f{i}.txt" for i in range(5)))
    r = await make_glob_tool(root_dir=tmp_path, max_results=3).handler(
        {"pattern": "*.txt"}, _ctx(),
    )
    lines = r.output.splitlines()
    assert lines[:3] == ["f0.txt", "f1.txt", "f2.txt"]
    assert "results truncated at 3 files" in lines[3]
    assert r.data["truncated"] is True
    assert r.data["count"] == 3


async def test_glob_excludes_noise_dirs_and_is_configurable(tmp_path: Path) -> None:
    """默认排除 .git / node_modules；自定义 exclude_dirs 生效。"""
    _touch(tmp_path, ".git/HEAD", "node_modules/m/index.js", "dist/app.js", "src/app.js")
    default = await make_glob_tool(root_dir=tmp_path).handler({"pattern": "**/*"}, _ctx())
    assert default.output.splitlines() == ["dist/app.js", "src/app.js"]
    custom = make_glob_tool(
        root_dir=tmp_path, exclude_dirs=DEFAULT_SEARCH_EXCLUDE_DIRS | {"dist"},
    )
    r = await custom.handler({"pattern": "**/*.js"}, _ctx())
    assert r.output.splitlines() == ["src/app.js"]


async def test_glob_rejects_path_escape(tmp_path: Path) -> None:
    """path 用 .. 或指向沙盒外的符号链接 → sandbox_violation。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    _touch(outside, "secret.txt")
    (root / "escape").symlink_to(outside)
    tool = make_glob_tool(root_dir=root)
    for path in ("..", "../outside", "escape"):
        r = await tool.handler({"pattern": "*", "path": path}, _ctx())
        assert r.is_error, path
        assert r.data["reason"] == "sandbox_violation"


async def test_glob_does_not_follow_symlink_escape_during_walk(tmp_path: Path) -> None:
    """遍历中指向沙盒外的链接不出现在结果里，且尾注告知跳过。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    _touch(root, "real.txt")
    _touch(outside, "secret.txt")
    (root / "escape_dir").symlink_to(outside)
    (root / "escape.txt").symlink_to(outside / "secret.txt")
    r = await make_glob_tool(root_dir=root).handler({"pattern": "**/*"}, _ctx())
    assert r.output.splitlines()[0] == "real.txt"
    assert "secret" not in r.output
    assert "skipped 2 symlink(s)" in r.output


async def test_glob_bad_pattern_and_not_a_directory(tmp_path: Path) -> None:
    """模式含 .. → bad_args；基点是文件 → not_found（not_a_directory）。"""
    _touch(tmp_path, "a.txt")
    tool = make_glob_tool(root_dir=tmp_path)
    bad = await tool.handler({"pattern": "../*"}, _ctx())
    assert bad.data["reason"] == "bad_args"
    not_dir = await tool.handler({"pattern": "*", "path": "a.txt"}, _ctx())
    assert not_dir.data["reason"] == "not_found"
    assert "not_a_directory" in not_dir.output


async def test_glob_permission_uses_file_read_scope(tmp_path: Path) -> None:
    """走 file_read 效果审批：target=基点绝对路径，metadata 带 call_id；拒绝 → permission_denied。"""
    _touch(tmp_path, "pkg/a.py")
    allow = _RecordingPolicy(grant=True)
    r = await make_glob_tool(root_dir=tmp_path, policy=allow).handler(
        {"pattern": "*.py", "path": "pkg"}, _ctx(),
    )
    assert not r.is_error
    [req] = allow.requests
    assert req.scope == "file_read"
    assert req.target == str((tmp_path / "pkg").resolve())
    assert req.metadata is not None
    assert req.metadata["call_id"] == "c1"
    assert req.metadata["tool"] == "glob"
    deny = _RecordingPolicy(grant=False)
    denied = await make_glob_tool(root_dir=tmp_path, policy=deny).handler(
        {"pattern": "*.py"}, _ctx(),
    )
    assert denied.is_error
    assert denied.data["reason"] == "permission_denied"


async def test_glob_cancelled_before_and_during_walk(tmp_path: Path) -> None:
    """预先取消 → 不遍历直接 cancelled；审批期间被取消 → 遍历在首个检查点停下。"""
    _touch(tmp_path, "a.txt")
    token = CancellationToken()
    token.cancel()
    r = await make_glob_tool(root_dir=tmp_path).handler({"pattern": "*"}, _ctx(token))
    assert r.data["reason"] == "cancelled"

    live = CancellationToken()

    class _CancellingPolicy:
        """审批通过的同时取消 token：模拟遍历开始前后到达的取消。"""

        async def check(self, request: PermissionRequest) -> PermissionDecision:
            live.cancel()
            return PermissionDecision.allow(reason="test")

    r2 = await make_glob_tool(root_dir=tmp_path, policy=_CancellingPolicy()).handler(
        {"pattern": "*"}, _ctx(live),
    )
    assert r2.is_error
    assert r2.data["reason"] == "cancelled"


def test_glob_spec_metadata_and_schema_rejects_unknown_args(tmp_path: Path) -> None:
    """只读并行安全 + pure/none；schema 带 additionalProperties=false，未知参数被预校验拒绝。"""
    spec = make_glob_tool(root_dir=tmp_path)
    assert spec.name == "glob"
    assert spec.parallel_safe is True
    assert (spec.effect_kind, spec.reconciliation) == ("pure", "none")
    assert spec.input_schema["additionalProperties"] is False
    feedback = check_tool_arguments(spec.input_schema, {"pattern": "*", "recursive": True})
    assert feedback is not None
    assert "recursive" in feedback
    assert check_tool_arguments(spec.input_schema, {"pattern": "*"}) is None
