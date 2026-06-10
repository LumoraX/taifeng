"""suspension-ttl 单测 —— 数据契约校验 / expires_at 派生 / resolver 到期裁决 /
engine 定时器(热武装 / 先核销者胜 / 冷重武装)。

对应 openspec change ``suspension-ttl-auto-adjudication``。
"""
from __future__ import annotations

import pytest

from taifeng.suspend.reason import PendingRequest, SuspendReason
from taifeng.suspend.record import SuspensionRecord
from taifeng.suspend.resolver import EXPIRE_SENTINEL, SuspensionResolver


def _rec(*reqs, created_at: int = 1000) -> SuspensionRecord:
    return SuspensionRecord(
        record_id="sr", thread_id="t", submission_id="s",
        turn_index=1, pending=tuple(reqs), created_at=created_at,
    )


# ---------- 构造期校验 ----------

def test_pending_validation_rejects_nonpositive_ttl():
    """ttl_seconds ≤ 0(含 -1 哨兵)构造期抛 ValueError。"""
    for bad in (0, -1, -100):
        with pytest.raises(ValueError, match="ttl_seconds"):
            PendingRequest(request_id="r", reason=SuspendReason.DATA,
                           ttl_seconds=bad)


def test_pending_validation_rejects_retry_on_human_input():
    """DATA / FORM / PERMISSION / CHILD_SKILL 禁 on_expire='retry'。"""
    for reason in (SuspendReason.DATA, SuspendReason.FORM,
                   SuspendReason.PERMISSION, SuspendReason.CHILD_SKILL):
        with pytest.raises(ValueError, match="on_expire"):
            PendingRequest(request_id="r", reason=reason,
                           ttl_seconds=60, on_expire="retry")


def test_pending_retry_allowed_on_system_reasons():
    """SYSTEM_RETRY / RESOURCE_LIMIT 可声明 on_expire='retry'。"""
    for reason in (SuspendReason.SYSTEM_RETRY, SuspendReason.RESOURCE_LIMIT):
        p = PendingRequest(request_id="r", reason=reason,
                           ttl_seconds=60, on_expire="retry")
        assert p.on_expire == "retry"


def test_default_no_ttl_zero_change():
    """默认不声明 ttl → None 永不过期,record 无到期时刻(零行为变化)。"""
    rec = _rec(PendingRequest(request_id="r", reason=SuspendReason.DATA))
    assert rec.expires_at is None


# ---------- expires_at 派生 + 序列化 round-trip ----------

def test_expires_at_takes_min_ttl():
    """record 到期时刻 = created_at + min(各 pending ttl);无 ttl 的不参与。"""
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.DATA, ttl_seconds=300),
        PendingRequest(request_id="r2", reason=SuspendReason.SYSTEM_RETRY,
                       ttl_seconds=60, on_expire="retry"),
        PendingRequest(request_id="r3", reason=SuspendReason.FORM),  # 无 ttl
        created_at=1000,
    )
    assert rec.expires_at == 1060


def test_ttl_fields_roundtrip_via_item():
    """ttl_seconds / on_expire 随 to_item 落盘、from_item 还原;expires_at 冷热一致。"""
    rec = _rec(PendingRequest(
        request_id="r1", reason=SuspendReason.RESOURCE_LIMIT,
        ttl_seconds=120, on_expire="retry"), created_at=500)
    back = SuspensionRecord.from_item(rec.to_item())
    assert back.pending[0].ttl_seconds == 120
    assert back.pending[0].on_expire == "retry"
    assert back.expires_at == 620


def test_old_jsonl_without_ttl_fields_loads():
    """旧 JSONL(pending 无 ttl 字段)装载 → 默认 None/abort,永不过期(前向兼容)。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.DATA))
    item = rec.to_item()
    # 模拟旧记录:剔除新字段
    for d in item.payload["pending"]:
        d.pop("ttl_seconds", None)
        d.pop("on_expire", None)
    back = SuspensionRecord.from_item(item)
    assert back.pending[0].ttl_seconds is None
    assert back.pending[0].on_expire == "abort"
    assert back.expires_at is None


# ---------- resolver 到期裁决(EXPIRE_SENTINEL) ----------

def _expire_all(rec: SuspensionRecord) -> dict:
    return {rid: {EXPIRE_SENTINEL: True} for rid in rec.request_ids()}


def test_expire_system_retry_with_retry():
    """SYSTEM_RETRY 到期 on_expire=retry → resample,不 abort(自动续跑)。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY,
                              ttl_seconds=60, on_expire="retry"))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.resample is True
    assert plan.abort is False


def test_expire_resource_limit_with_retry_no_resample():
    """RESOURCE_LIMIT 到期 retry → 不置 resample(重建续跑即继续循环)、不 abort。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.RESOURCE_LIMIT,
                              ttl_seconds=60, on_expire="retry"))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.resample is False
    assert plan.abort is False


def test_expire_data_form_permission_abort_with_gap_fill():
    """人类输入类到期 → 悬空 fc 回填 suspension_expired error + 整体 abort。"""
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.DATA,
                       related_call_id="ca", ttl_seconds=60),
        PendingRequest(request_id="r2", reason=SuspendReason.PERMISSION,
                       related_call_id="cb", ttl_seconds=60),
    )
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.deny_outputs["ca"] == "suspension_expired"
    assert plan.deny_outputs["cb"] == "suspension_expired"
    assert plan.abort is True
    assert plan.execute_tool_call_ids == []


def test_expire_mixed_record_abort_wins():
    """混合 record(retry 系统位 + 人类输入)到期 → abort 胜出(record 级一次性)。"""
    rec = _rec(
        PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY,
                       ttl_seconds=60, on_expire="retry"),
        PendingRequest(request_id="r2", reason=SuspendReason.FORM,
                       related_call_id="cf", ttl_seconds=60),
    )
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.abort is True


def test_expire_system_abort_default():
    """SYSTEM_RETRY 到期默认 on_expire=abort → abort 不 resample。"""
    rec = _rec(PendingRequest(request_id="r1", reason=SuspendReason.SYSTEM_RETRY,
                              ttl_seconds=60))
    plan = SuspensionResolver().plan(rec, _expire_all(rec))
    assert plan.abort is True
    assert plan.resample is False
