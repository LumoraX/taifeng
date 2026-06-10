"""PinnedStateSource 协议 + PinnedStateRegistry 单元测试。

覆盖:注册/同名拒绝/注销/迭代序、render_all 的 per-source 截断、
总预算丢弃、None 跳过、渲染异常捕获。
"""

from __future__ import annotations

import pytest

from taifeng.context.pinned_state import PinnedStateRegistry


class _Src:
    """测试用 source:可配置渲染结果(文本 / None / 抛异常)。"""

    def __init__(self, name: str, text: str | None, *, max_chars: int = 1000,
                 boom: bool = False) -> None:
        self.name = name
        self.max_chars = max_chars
        self._text = text
        self._boom = boom

    def format_for_injection(self) -> str | None:
        if self._boom:
            raise RuntimeError("render exploded")
        return self._text


def test_register_and_iterate_keeps_order():
    """注册序即迭代序(总预算按此序优先)。"""
    reg = PinnedStateRegistry()
    a, b = _Src("a", "x"), _Src("b", "y")
    reg.register(a)
    reg.register(b)
    assert [s.name for s in reg] == ["a", "b"]


def test_register_duplicate_name_raises():
    """同名注册显式 ValueError,禁静默覆盖。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("dup", "x"))
    with pytest.raises(ValueError, match="dup"):
        reg.register(_Src("dup", "y"))


def test_unregister_removes_and_missing_raises():
    """注销后不再迭代;注销不存在的名字显式 KeyError(非 silent)。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("a", "x"))
    reg.unregister("a")
    assert list(reg) == []
    with pytest.raises(KeyError):
        reg.unregister("a")


def test_render_all_basic():
    """正常渲染:每个 source 一条,按注册序。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("plan", "step1"))
    reg.register(_Src("note", "memo"))
    out = reg.render_all()
    assert [(e.name, e.text) for e in out.entries] == [
        ("plan", "step1"), ("note", "memo")]
    assert out.dropped == []
    assert out.errors == []


def test_render_all_per_source_truncate():
    """单 source 超 max_chars → truncate_middle 截断(保头尾)。"""
    reg = PinnedStateRegistry()
    long_text = "H" * 300 + "T" * 300
    reg.register(_Src("big", long_text, max_chars=100))
    out = reg.render_all()
    (entry,) = out.entries
    assert len(entry.text) <= 100 + 40  # marker 数字位宽允许微小偏差
    assert entry.text.startswith("H")
    assert entry.text.endswith("T")


def test_render_all_total_budget_drops_whole_source():
    """总预算装不下的 source 整体丢弃,记录到 dropped(不静默)。"""
    reg = PinnedStateRegistry(total_max_chars=120)
    reg.register(_Src("first", "a" * 100))
    reg.register(_Src("second", "b" * 50))   # 100+50 > 120 → 整体丢
    reg.register(_Src("third", "c" * 10))    # 100+10 ≤ 120 → 仍可入
    out = reg.render_all()
    assert [e.name for e in out.entries] == ["first", "third"]
    assert out.dropped == ["second"]


def test_render_all_none_is_skipped_silently():
    """渲染 None = 本次不注入,既不进 entries 也不算 dropped。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("quiet", None))
    reg.register(_Src("loud", "hi"))
    out = reg.render_all()
    assert [e.name for e in out.entries] == ["loud"]
    assert out.dropped == []


def test_render_all_exception_captured():
    """单 source 渲染异常 → 捕获进 errors,其余 source 不受影响。"""
    reg = PinnedStateRegistry()
    reg.register(_Src("bomb", None, boom=True))
    reg.register(_Src("ok", "fine"))
    out = reg.render_all()
    assert [e.name for e in out.entries] == ["ok"]
    assert len(out.errors) == 1
    assert out.errors[0][0] == "bomb"
    assert "render exploded" in out.errors[0][1]
