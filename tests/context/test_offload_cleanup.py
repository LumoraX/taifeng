"""OffloadStrategy 的 thread 级联清理(v1 生命周期管理)。

spec:offload 文件按 {thread_id} 目录组织;thread/conversation 删除时级联清理
该目录。v1 不做 TTL / 容量上限。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from taifeng.context.strategies import OffloadStrategy

if TYPE_CHECKING:
    from pathlib import Path


async def test_cleanup_thread_removes_offload_dir(tmp_path: Path) -> None:
    """清理某 thread → 其 _offload/{thread_id} 目录被删除。"""
    strat = OffloadStrategy(file_root=tmp_path)
    tdir = tmp_path / "_offload" / "t1"
    tdir.mkdir(parents=True)
    (tdir / "c1").write_text("payload", encoding="utf-8")

    await strat.cleanup_thread("t1")
    assert not tdir.exists()


async def test_cleanup_thread_isolated(tmp_path: Path) -> None:
    """只清目标 thread,其它 thread 的 offload 文件不受影响。"""
    strat = OffloadStrategy(file_root=tmp_path)
    for tid in ("t1", "t2"):
        d = tmp_path / "_offload" / tid
        d.mkdir(parents=True)
        (d / "c1").write_text("p", encoding="utf-8")

    await strat.cleanup_thread("t1")
    assert not (tmp_path / "_offload" / "t1").exists()
    assert (tmp_path / "_offload" / "t2" / "c1").is_file()


async def test_cleanup_missing_thread_is_noop(tmp_path: Path) -> None:
    """清理不存在的 thread 目录不报错(幂等)。"""
    strat = OffloadStrategy(file_root=tmp_path)
    await strat.cleanup_thread("never-existed")  # 不抛异常即通过
