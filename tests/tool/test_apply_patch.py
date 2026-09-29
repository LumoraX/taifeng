"""apply_patch 工具测试（M4 / tool-builtins-extended）。

覆盖 spec ``tool-builtins-extended`` Requirement "apply_patch 工具原子化结构化补丁应用"
的 7 个 Scenario。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng.loop.cancellation import CancellationToken
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
)
from taifeng.tool.builtins.apply_patch import make_apply_patch_tool
from taifeng.tool.spec import ToolContext

if TYPE_CHECKING:
    from pathlib import Path


def _ctx() -> ToolContext:
    return ToolContext(call_id="c1", cancel=CancellationToken(), thread_id="t1")


def _capturing_policy(
    captured: list[PermissionRequest],
    *,
    deny_targets: tuple[str, ...] = (),
) -> PermissionPolicy:
    """构造记录全部请求的策略；target 命中 ``deny_targets`` 的请求被拒。"""

    async def check(request: PermissionRequest) -> PermissionDecision:
        captured.append(request)
        if request.target in deny_targets:
            return PermissionDecision.deny(reason="test_deny")
        return PermissionDecision.allow(reason="test")

    return PermissionPolicy(default_mode="ask", prompter=CallbackPrompter(check))


# --------------------------------------------------------------------
# 权限：按路径发 file_write（效果模型，ADR 0028 backlog → ADR 0073）
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_patch_requests_file_write_per_path(tmp_path: Path) -> None:
    """每个被改动的路径各发一条 file_write 请求，target 是解析后的绝对路径。"""
    (tmp_path / "a.txt").write_text("old", encoding="utf-8")
    (tmp_path / "gone.txt").write_text("x", encoding="utf-8")
    captured: list[PermissionRequest] = []
    spec = make_apply_patch_tool(
        root_dir=tmp_path, policy=_capturing_policy(captured),
    )

    result = await spec.handler(
        {"patches": [
            {"path": "new.txt", "new_text": "ok", "create": True},
            {"path": "a.txt", "old_text": "old", "new_text": "new"},
            {"path": "gone.txt", "delete": True},
        ]},
        _ctx(),
    )

    assert not result.is_error, result.output
    root = tmp_path.resolve()
    assert [(r.scope, r.target) for r in captured] == [
        ("file_write", str(root / "new.txt")),
        ("file_write", str(root / "a.txt")),
        ("file_write", str(root / "gone.txt")),
    ]
    assert [r.metadata["patch_kinds"] for r in captured] == [
        ["create"], ["edit"], ["delete"],
    ]
    assert all(r.metadata["call_id"] == "c1" for r in captured)
    assert all(r.metadata["thread_id"] == "t1" for r in captured)
    assert all(r.metadata["tool"] == "apply_patch" for r in captured)


@pytest.mark.asyncio
async def test_apply_patch_same_path_requests_once(tmp_path: Path) -> None:
    """同一路径的多条 patch 只审批一次，patch_kinds 按出现顺序汇总。"""
    (tmp_path / "a.txt").write_text("one two", encoding="utf-8")
    captured: list[PermissionRequest] = []
    spec = make_apply_patch_tool(
        root_dir=tmp_path, policy=_capturing_policy(captured),
    )

    result = await spec.handler(
        {"patches": [
            {"path": "a.txt", "old_text": "one", "new_text": "1"},
            {"path": "./a.txt", "old_text": "two", "new_text": "2"},
        ]},
        _ctx(),
    )

    assert not result.is_error, result.output
    assert [(r.scope, r.target) for r in captured] == [
        ("file_write", str(tmp_path.resolve() / "a.txt")),
    ]
    assert captured[0].metadata["patch_kinds"] == ["edit", "edit"]


@pytest.mark.asyncio
async def test_apply_patch_path_deny_rule_blocks_whole_group(
    tmp_path: Path,
) -> None:
    """按路径写的 deny 规则能拦住 apply_patch，且整组 0 文件被改（原子）。"""
    (tmp_path / "protected").mkdir()
    secret = tmp_path / "protected" / "conf.txt"
    secret.write_text("keep", encoding="utf-8")
    root = tmp_path.resolve()
    policy = PermissionPolicy.from_dict({
        "default_mode": "allow",
        "deny": [f"FileWrite({root}/protected/*)"],
    })
    spec = make_apply_patch_tool(root_dir=tmp_path, policy=policy)

    result = await spec.handler(
        {"patches": [
            {"path": "ok.txt", "new_text": "fine", "create": True},
            {"path": "protected/conf.txt", "old_text": "keep", "new_text": "x"},
        ]},
        _ctx(),
    )

    assert result.is_error
    assert result.data["reason"] == "permission_denied"
    assert result.data["denied_path"] == str(root / "protected" / "conf.txt")
    assert not (tmp_path / "ok.txt").exists()
    assert secret.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_apply_patch_denied_stops_asking_remaining_paths(
    tmp_path: Path,
) -> None:
    """首个被拒路径即终止审批，不再为后续路径打扰审批人。"""
    root = tmp_path.resolve()
    captured: list[PermissionRequest] = []
    spec = make_apply_patch_tool(
        root_dir=tmp_path,
        policy=_capturing_policy(captured, deny_targets=(str(root / "a.txt"),)),
    )

    result = await spec.handler(
        {"patches": [
            {"path": "a.txt", "new_text": "1", "create": True},
            {"path": "b.txt", "new_text": "2", "create": True},
        ]},
        _ctx(),
    )

    assert result.is_error
    assert result.data["reason"] == "permission_denied"
    assert [r.target for r in captured] == [str(root / "a.txt")]
    assert not (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()


@pytest.mark.asyncio
async def test_apply_patch_sandbox_violation_never_asks_permission(
    tmp_path: Path,
) -> None:
    """越出沙盒的路径在审批前就被拒，不向审批人展示任何请求。"""
    captured: list[PermissionRequest] = []
    spec = make_apply_patch_tool(
        root_dir=tmp_path, policy=_capturing_policy(captured),
    )

    result = await spec.handler(
        {"patches": [
            {"path": "ok.txt", "new_text": "fine", "create": True},
            {"path": "../escape.txt", "new_text": "x", "create": True},
        ]},
        _ctx(),
    )

    assert result.is_error
    assert result.data["reason"] == "patch_validation_failed"
    assert result.data["patch_index"] == 1
    assert captured == []
    assert not (tmp_path / "ok.txt").exists()


@pytest.mark.asyncio
async def test_apply_patch_denied_before_reading_file_content(
    tmp_path: Path,
) -> None:
    """审批先于内容校验：被拒时不得泄露目标文件里有没有 old_text。"""
    target = tmp_path / "a.txt"
    target.write_text("content", encoding="utf-8")
    captured: list[PermissionRequest] = []
    spec = make_apply_patch_tool(
        root_dir=tmp_path,
        policy=_capturing_policy(
            captured, deny_targets=(str(tmp_path.resolve() / "a.txt"),),
        ),
    )

    result = await spec.handler(
        {"patches": [
            {"path": "a.txt", "old_text": "absent", "new_text": "x"},
        ]},
        _ctx(),
    )

    assert result.is_error
    assert result.data["reason"] == "permission_denied"
    assert "old_text_not_found" not in result.output


# --------------------------------------------------------------------
# Scenario: edit / create / delete 成功路径
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_edit_patch_succeeds(tmp_path: Path) -> None:
    f = tmp_path / "foo.py"
    f.write_text("def f(x): return x", encoding="utf-8")
    spec = make_apply_patch_tool(root_dir=tmp_path)

    r = await spec.handler(
        {"patches": [{
            "path": "foo.py",
            "old_text": "def f(x): return x",
            "new_text": "def f(x): return x + 1",
        }]},
        _ctx(),
    )
    assert not r.is_error, r.output
    assert r.data["applied"][0]["kind"] == "edit"
    assert f.read_text(encoding="utf-8") == "def f(x): return x + 1"


@pytest.mark.asyncio
async def test_apply_create_patch_succeeds(tmp_path: Path) -> None:
    spec = make_apply_patch_tool(root_dir=tmp_path)
    r = await spec.handler(
        {"patches": [{
            "path": "new.py",
            "new_text": "hello\n",
            "create": True,
        }]},
        _ctx(),
    )
    assert not r.is_error, r.output
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_apply_delete_patch_succeeds(tmp_path: Path) -> None:
    f = tmp_path / "obsolete.py"
    f.write_text("garbage", encoding="utf-8")
    spec = make_apply_patch_tool(root_dir=tmp_path)
    r = await spec.handler(
        {"patches": [{"path": "obsolete.py", "delete": True}]},
        _ctx(),
    )
    assert not r.is_error, r.output
    assert not f.exists()


# --------------------------------------------------------------------
# Scenario: 原子性（任一 phase 1 失败 → 0 文件被改）
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_atomic_all_or_nothing(tmp_path: Path) -> None:
    """第 2 个 patch 校验失败，第 1 个不该被应用。"""
    file_a = tmp_path / "a.py"
    file_a.write_text("hello a", encoding="utf-8")
    file_b = tmp_path / "b.py"
    file_b.write_text("hello b", encoding="utf-8")

    spec = make_apply_patch_tool(root_dir=tmp_path)
    r = await spec.handler(
        {"patches": [
            {"path": "a.py", "old_text": "hello a", "new_text": "modified a"},
            # B 的 old_text 不存在 → phase 1 fail
            {"path": "b.py", "old_text": "NONEXISTENT", "new_text": "won't apply"},
        ]},
        _ctx(),
    )
    assert r.is_error
    assert r.data["reason"] == "patch_validation_failed"
    assert r.data["patch_index"] == 1
    # A 应保持原样（原子语义核心）
    assert file_a.read_text(encoding="utf-8") == "hello a"
    assert file_b.read_text(encoding="utf-8") == "hello b"


# --------------------------------------------------------------------
# Scenario: 多次出现 / 沙盒 / create 已存在
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_old_text_ambiguous_fails(tmp_path: Path) -> None:
    f = tmp_path / "dup.py"
    f.write_text("x\nx\nx\n", encoding="utf-8")
    spec = make_apply_patch_tool(root_dir=tmp_path)

    r = await spec.handler(
        {"patches": [{"path": "dup.py", "old_text": "x", "new_text": "y"}]},
        _ctx(),
    )
    assert r.is_error
    assert "ambiguous_old_text" in r.data["error"]
    # 文件未被修改
    assert f.read_text(encoding="utf-8") == "x\nx\nx\n"


@pytest.mark.asyncio
async def test_apply_path_outside_sandbox_fails(tmp_path: Path) -> None:
    spec = make_apply_patch_tool(root_dir=tmp_path)
    r = await spec.handler(
        {"patches": [{
            "path": "../escape.py",
            "new_text": "evil",
            "create": True,
        }]},
        _ctx(),
    )
    assert r.is_error
    assert "sandbox_violation" in r.data["error"]


@pytest.mark.asyncio
async def test_apply_create_existing_path_fails(tmp_path: Path) -> None:
    f = tmp_path / "exists.py"
    f.write_text("original", encoding="utf-8")
    spec = make_apply_patch_tool(root_dir=tmp_path)

    r = await spec.handler(
        {"patches": [{
            "path": "exists.py",
            "new_text": "overwrite",
            "create": True,
        }]},
        _ctx(),
    )
    assert r.is_error
    assert "path_exists" in r.data["error"]
    # 原文件保持
    assert f.read_text(encoding="utf-8") == "original"
