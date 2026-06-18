"""skill/verify.py 单元测试：验证数据契约形状 + LlmSkillVerifier 行为。

本任务覆盖：
    - VerifiedCandidate 契约形状（frozen + 长相/适配分离字段）。
    - SkillVerifier 协议结构兼容。
    - LlmSkillVerifier：合法解析、池外丢弃、整体失败抛错、单项脏丢弃、
      verify_max_candidates 截断、body 截断标记、get_body 返回 None 处理、
      cancel 语义、空 candidates 短路。

LLM 调用全部走 SimClient（脚本化回放），CI 内禁止真实 API。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json as _json
from collections.abc import Callable, Sequence

import pytest

from taifeng.llm.providers.sim.client import SimClient
from taifeng.llm.providers.sim.script import SimTurn
from taifeng.loop.cancellation import CancellationToken
from taifeng.skill.recall import SkillCandidate
from taifeng.skill.verify import (
    LlmSkillVerifier,
    SkillVerifier,
    SkillVerifyParseError,
    VerifiedCandidate,
)


# ----------------------------------------------------------------------------
# 测试夹具：候选池 + get_body 工厂 + SimClient 工厂
# ----------------------------------------------------------------------------


def _candidate(skill_id: str, *, confidence: float = 0.8) -> SkillCandidate:
    """构造一条召回候选（verify 的输入），description 与 id 关联便于断言。"""
    return SkillCandidate(
        skill_id=skill_id,
        description=f"{skill_id} 的描述",
        score=confidence,
        confidence=confidence,
        matched_snippet=None,
    )


def _get_body_from(bodies: dict[str, str]) -> Callable[[str], str | None]:
    """据 dict 构造 get_body 回调：命中返回 body，缺失返回 None（不伪造）。"""

    def _get(skill_id: str) -> str | None:
        return bodies.get(skill_id)

    return _get


def _sim_with_text(text: str) -> SimClient:
    """构造固定返回 text 的 SimClient（单 turn，无 tool_call）。"""
    return SimClient(turns=[SimTurn(text=text)])


def _prompt_text(sim: SimClient) -> str | None:
    """取 sim 最近一次请求的规范化全文（含 system + user）；尚无请求返回 None。"""
    rec = sim.ledger.last_request()
    return rec.blob() if rec is not None else None


# ----------------------------------------------------------------------------
# A. 数据契约形状
# ----------------------------------------------------------------------------


def test_verified_candidate_is_frozen() -> None:
    """VerifiedCandidate 为 frozen dataclass：recall_confidence / verify_confidence 分离。"""
    vc = VerifiedCandidate(
        skill_id="analyzer",
        description="分析患者数据",
        recall_confidence=0.9,
        applicable=True,
        verify_confidence=0.75,
        reason="输入齐全",
    )
    # 长相（recall）与适配（verify）是两个独立字段
    assert vc.recall_confidence == 0.9
    assert vc.verify_confidence == 0.75
    assert vc.applicable is True
    assert vc.reason == "输入齐全"
    # frozen：改字段必须抛 FrozenInstanceError
    with pytest.raises(dataclasses.FrozenInstanceError):
        vc.applicable = False  # type: ignore[misc]


def test_verified_candidate_public_import() -> None:
    """契约从 taifeng.skill.verify 可导入。"""
    from taifeng.skill.verify import (  # noqa: PLC0415
        LlmSkillVerifier,
        SkillVerifier,
        SkillVerifyParseError,
        VerifiedCandidate,
    )

    assert VerifiedCandidate is not None
    assert SkillVerifier is not None
    assert LlmSkillVerifier is not None
    assert SkillVerifyParseError is not None


# ----------------------------------------------------------------------------
# B. 协议结构兼容
# ----------------------------------------------------------------------------


def test_skill_verifier_protocol_accepts_stub() -> None:
    """最小 stub 满足 SkillVerifier 协议（structural + runtime_checkable）。"""

    class _StubVerifier:
        async def verify(
            self,
            task: str,
            candidates: Sequence[SkillCandidate],
            *,
            get_body: Callable[[str], str | None],
            cancel: CancellationToken,
        ) -> list[VerifiedCandidate]:
            """桩实现：全部判 applicable=True。"""
            return [
                VerifiedCandidate(
                    skill_id=c.skill_id,
                    description=c.description,
                    recall_confidence=c.confidence,
                    applicable=True,
                    verify_confidence=1.0,
                    reason="stub",
                )
                for c in candidates
            ]

    stub = _StubVerifier()
    assert isinstance(stub, SkillVerifier)


def test_llm_verifier_is_skill_verifier() -> None:
    """LlmSkillVerifier 是 SkillVerifier 协议的合法实现。"""
    verifier = LlmSkillVerifier(model_client=_sim_with_text("[]"))
    assert isinstance(verifier, SkillVerifier)


# ----------------------------------------------------------------------------
# C. LlmSkillVerifier 行为
# ----------------------------------------------------------------------------


async def test_verify_parses_valid_json_only_applicable() -> None:
    """合法 JSON（applicable=true/false 混合）→ 只返回 applicable=true 的。"""
    candidates = [_candidate("finance", confidence=0.9), _candidate("weather", confidence=0.5)]
    bodies = {"finance": "finance body", "weather": "weather body"}
    answer = _json.dumps(
        [
            {"skill_id": "finance", "applicable": True, "confidence": 0.8, "reason": "输入满足"},
            {"skill_id": "weather", "applicable": False, "confidence": 0.2, "reason": "缺地理坐标"},
        ]
    )
    verifier = LlmSkillVerifier(model_client=_sim_with_text(answer))
    result = await verifier.verify(
        "报销问题",
        candidates,
        get_body=_get_body_from(bodies),
        cancel=CancellationToken(name="t"),
    )
    # 只剩 applicable=True 的 finance
    assert [vc.skill_id for vc in result] == ["finance"]
    vc = result[0]
    assert vc.applicable is True
    assert vc.verify_confidence == pytest.approx(0.8)
    assert vc.reason == "输入满足"
    # description / recall_confidence 来自入参 candidate（不信模型复述）
    assert vc.description == "finance 的描述"
    assert vc.recall_confidence == pytest.approx(0.9)


async def test_verify_drops_out_of_pool_id() -> None:
    """模型编了候选外 id → 该项被丢弃。"""
    candidates = [_candidate("finance")]
    answer = _json.dumps(
        [
            {"skill_id": "finance", "applicable": True, "confidence": 0.7, "reason": "ok"},
            {"skill_id": "hallucinated", "applicable": True, "confidence": 0.9, "reason": "ok"},
        ]
    )
    verifier = LlmSkillVerifier(model_client=_sim_with_text(answer))
    result = await verifier.verify(
        "任务",
        candidates,
        get_body=_get_body_from({"finance": "body"}),
        cancel=CancellationToken(name="t"),
    )
    assert [vc.skill_id for vc in result] == ["finance"]


async def test_verify_invalid_json_raises() -> None:
    """非法 JSON → 抛 SkillVerifyParseError。"""
    candidates = [_candidate("finance")]
    verifier = LlmSkillVerifier(model_client=_sim_with_text("这不是 JSON"))
    with pytest.raises(SkillVerifyParseError):
        await verifier.verify(
            "任务",
            candidates,
            get_body=_get_body_from({"finance": "body"}),
            cancel=CancellationToken(name="t"),
        )


async def test_verify_json_not_array_raises() -> None:
    """合法 JSON 但顶层非数组 → 抛 SkillVerifyParseError。"""
    candidates = [_candidate("finance")]
    verifier = LlmSkillVerifier(model_client=_sim_with_text('{"skill_id": "finance"}'))
    with pytest.raises(SkillVerifyParseError):
        await verifier.verify(
            "任务",
            candidates,
            get_body=_get_body_from({"finance": "body"}),
            cancel=CancellationToken(name="t"),
        )


async def test_verify_drops_item_missing_fields() -> None:
    """单项缺字段（缺 reason / 缺 confidence）→ 丢弃不伪造。"""
    candidates = [_candidate("a"), _candidate("b"), _candidate("c")]
    answer = _json.dumps(
        [
            {"skill_id": "a", "applicable": True, "confidence": 0.7, "reason": "ok"},
            {"skill_id": "b", "applicable": True, "confidence": 0.7},  # 缺 reason
            {"skill_id": "c", "applicable": True, "reason": "ok"},  # 缺 confidence
        ]
    )
    bodies = {"a": "ba", "b": "bb", "c": "bc"}
    verifier = LlmSkillVerifier(model_client=_sim_with_text(answer))
    result = await verifier.verify(
        "任务", candidates, get_body=_get_body_from(bodies), cancel=CancellationToken(name="t")
    )
    assert [vc.skill_id for vc in result] == ["a"]


async def test_verify_drops_confidence_out_of_range() -> None:
    """confidence 越界 [0,1] → 丢弃不裁剪伪造。"""
    candidates = [_candidate("a"), _candidate("b")]
    answer = _json.dumps(
        [
            {"skill_id": "a", "applicable": True, "confidence": 1.5, "reason": "越界"},
            {"skill_id": "b", "applicable": True, "confidence": 0.6, "reason": "ok"},
        ]
    )
    bodies = {"a": "ba", "b": "bb"}
    verifier = LlmSkillVerifier(model_client=_sim_with_text(answer))
    result = await verifier.verify(
        "任务", candidates, get_body=_get_body_from(bodies), cancel=CancellationToken(name="t")
    )
    assert [vc.skill_id for vc in result] == ["b"]


async def test_verify_sorted_by_confidence_desc_then_id() -> None:
    """返回按 verify_confidence 降序、同分按 skill_id 升序（确定性排序）。"""
    candidates = [_candidate("zzz"), _candidate("aaa"), _candidate("mmm")]
    answer = _json.dumps(
        [
            {"skill_id": "zzz", "applicable": True, "confidence": 0.5, "reason": "r"},
            {"skill_id": "aaa", "applicable": True, "confidence": 0.5, "reason": "r"},
            {"skill_id": "mmm", "applicable": True, "confidence": 0.9, "reason": "r"},
        ]
    )
    bodies = {"zzz": "b", "aaa": "b", "mmm": "b"}
    verifier = LlmSkillVerifier(model_client=_sim_with_text(answer))
    result = await verifier.verify(
        "任务", candidates, get_body=_get_body_from(bodies), cancel=CancellationToken(name="t")
    )
    # mmm 最高分在前；aaa / zzz 同分按 skill_id 升序
    assert [vc.skill_id for vc in result] == ["mmm", "aaa", "zzz"]


async def test_verify_max_candidates_truncation() -> None:
    """候选数多于 verify_max_candidates 时只验前 N（prompt 不含被截掉的候选）。"""
    candidates = [_candidate(f"s{i}") for i in range(5)]
    bodies = {f"s{i}": f"body-{i}" for i in range(5)}
    # 模型对全部 5 个都判 applicable，但只有前 2 个会进 prompt
    answer = _json.dumps(
        [{"skill_id": f"s{i}", "applicable": True, "confidence": 0.5, "reason": "r"} for i in range(5)]
    )
    sim = _sim_with_text(answer)
    verifier = LlmSkillVerifier(model_client=sim, verify_max_candidates=2)
    result = await verifier.verify(
        "任务", candidates, get_body=_get_body_from(bodies), cancel=CancellationToken(name="t")
    )
    # 只验前 2 个：s3 / s4 即便模型返回也因不在「已验池」内被丢弃
    assert {vc.skill_id for vc in result} == {"s0", "s1"}
    # prompt 里只应出现前 2 个候选 id（s2..s4 被截断，未进 prompt）
    prompt_text = _prompt_text(sim)
    assert prompt_text is not None
    assert "s0" in prompt_text and "s1" in prompt_text
    assert "s3" not in prompt_text and "s4" not in prompt_text


async def test_verify_body_truncation_marks_reason() -> None:
    """单 body 超 verify_body_char_limit → 截断且 reason 注明 body 截断。"""
    candidates = [_candidate("big")]
    long_body = "x" * 100
    answer = _json.dumps(
        [{"skill_id": "big", "applicable": True, "confidence": 0.7, "reason": "ok"}]
    )
    sim = _sim_with_text(answer)
    verifier = LlmSkillVerifier(model_client=sim, verify_body_char_limit=10)
    result = await verifier.verify(
        "任务", candidates, get_body=_get_body_from({"big": long_body}), cancel=CancellationToken(name="t")
    )
    assert len(result) == 1
    # 截断标记进 reason，便于审计
    assert "body 已截断" in result[0].reason
    # prompt 里 body 被截到 ≤ limit（原文 100 字符不会整段出现）
    prompt_text = _prompt_text(sim)
    assert prompt_text is not None
    assert long_body not in prompt_text


async def test_verify_missing_body_marked_not_applicable() -> None:
    """get_body 返回 None 的候选 → 标 applicable=False（不进 prompt、不伪造）。

    策略：无 body 无法验证输入要求，直接判不适用，不送 LLM，结果按 applicable=True
    过滤后该候选自然不出现在返回里。
    """
    candidates = [_candidate("has_body"), _candidate("no_body")]
    # 只有 has_body 有 body；no_body 取不到
    bodies = {"has_body": "body"}
    answer = _json.dumps(
        [{"skill_id": "has_body", "applicable": True, "confidence": 0.7, "reason": "ok"}]
    )
    sim = _sim_with_text(answer)
    verifier = LlmSkillVerifier(model_client=sim)
    result = await verifier.verify(
        "任务", candidates, get_body=_get_body_from(bodies), cancel=CancellationToken(name="t")
    )
    # no_body 因无 body 被判不适用，不进结果
    assert [vc.skill_id for vc in result] == ["has_body"]
    # no_body 未进 prompt（不浪费 token 验一个拿不到 body 的候选）
    prompt_text = _prompt_text(sim)
    assert prompt_text is not None
    assert "no_body" not in prompt_text


async def test_verify_empty_candidates_no_llm_call() -> None:
    """空 candidates → 返回空、不发起 LLM 调用。"""
    sim = _sim_with_text("[]")
    verifier = LlmSkillVerifier(model_client=sim)
    result = await verifier.verify(
        "任务", [], get_body=_get_body_from({}), cancel=CancellationToken(name="t")
    )
    assert result == []
    # 未发起任何调用：last_request 为 None
    assert _prompt_text(sim) is None


async def test_verify_all_missing_body_no_llm_call() -> None:
    """全部候选都拿不到 body → 无可验候选，短路不发 LLM 调用。"""
    candidates = [_candidate("a"), _candidate("b")]
    sim = _sim_with_text("[]")
    verifier = LlmSkillVerifier(model_client=sim)
    result = await verifier.verify(
        "任务", candidates, get_body=_get_body_from({}), cancel=CancellationToken(name="t")
    )
    assert result == []
    assert _prompt_text(sim) is None


async def test_verify_cancel_raises() -> None:
    """cancel：传入已取消 token → 抛 asyncio.CancelledError（R4 长调用可中断）。"""
    candidates = [_candidate("finance")]
    verifier = LlmSkillVerifier(model_client=_sim_with_text("[]"))
    cancel = CancellationToken(name="t")
    cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await verifier.verify(
            "任务",
            candidates,
            get_body=_get_body_from({"finance": "body"}),
            cancel=cancel,
        )
