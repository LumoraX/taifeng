"""DoomLoopDetector 纯逻辑 —— 连续相同 (tool,args) 成功调用的「先警后断」检测。"""
from __future__ import annotations

from taifeng.loop.doom_loop import DoomLoopConfig, DoomLoopDetector


def _det(n: int | None = 3) -> DoomLoopDetector:
    return DoomLoopDetector(DoomLoopConfig(max_consecutive_repeats=n))


def test_disabled_when_threshold_none():
    """阈值 None = 不启用：任何重复都不触发（opt-in，行为零变化）。"""
    det = _det(None)
    for _ in range(100):
        assert det.record("file_read", '{"path":"a"}') == "none"
    assert det.opened is False


def test_warns_at_threshold_once():
    """连续达 N 次同签名 → 恰好第 N 次返回 warn；此后到 2N 前不再 warn。"""
    det = _det(3)
    assert det.record("read", '{"p":"x"}') == "none"   # 1
    assert det.record("read", '{"p":"x"}') == "none"   # 2
    assert det.record("read", '{"p":"x"}') == "warn"    # 3 == N
    assert det.record("read", '{"p":"x"}') == "none"   # 4
    assert det.record("read", '{"p":"x"}') == "none"   # 5
    assert det.opened is False


def test_opens_at_double_threshold():
    """警告后仍连续重复，到 2N 次 → open（断路闩锁）。"""
    det = _det(3)
    actions = [det.record("sh", '{"cmd":"ls"}') for _ in range(6)]
    assert actions[2] == "warn"   # 第 3 次警
    assert actions[5] == "open"   # 第 6 次 == 2N 断
    assert det.opened is True


def test_opened_latches_then_none():
    """open 后闩锁：后续记录恒 none（单次触发，调用方据 open emit 一次）。"""
    det = _det(2)
    [det.record("t", "{}") for _ in range(4)]  # 2 警、4 断
    assert det.opened is True
    assert det.record("t", "{}") == "none"


def test_different_signature_resets_streak_and_warn():
    """中途出现不同 (tool,args) → 连续计数重置、warned 清零（模型已跳出）。"""
    det = _det(3)
    det.record("read", '{"p":"x"}')          # 1
    det.record("read", '{"p":"x"}')          # 2
    assert det.record("read", '{"p":"y"}') == "none"   # 不同 args → 重置为 1
    assert det.record("read", '{"p":"y"}') == "none"   # 2
    assert det.record("read", '{"p":"y"}') == "warn"    # 3 → 新一轮警
    assert det.opened is False


def test_same_tool_different_tool_resets():
    """工具名不同也算不同签名（签名 = tool + args 原串）。"""
    det = _det(2)
    det.record("a", "{}")                     # 1
    assert det.record("b", "{}") == "none"    # 不同 tool → 重置为 1
    assert det.record("b", "{}") == "warn"     # 2 → 警


def test_snapshot_carries_tool_and_counts_no_args_body():
    """snapshot 只带工具名 + 计数 + 阈值，不带 args 正文（PII 约束）。"""
    det = _det(2)
    det.record("danger", '{"secret":"xyz"}')
    det.record("danger", '{"secret":"xyz"}')
    snap = det.snapshot()
    assert snap["tool"] == "danger"
    assert snap["consecutive"] == 2
    assert snap["threshold"] == 2
    assert "xyz" not in str(snap), "snapshot 不得泄漏 args 正文"
