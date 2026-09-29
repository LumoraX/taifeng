"""可声明的失败恢复配方（ADR 0084）。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, get_args

import pytest

import taifeng
from taifeng.llm.errors import ContentFilterError, FailureClass
from taifeng.llm.providers import SimClient, SimTurn
from taifeng.llm.recovery import (
    RecoveryPlan,
    RecoveryRecipeBook,
    RecoveryStep,
    recommend_recovery,
)
from taifeng.loop.failure_policy import (
    ConservativeFailurePolicy,
    FailureContext,
    FailureDisposition,
    RecipeDeclaringPolicy,
    RecoveryRecipeProvider,
    SuspendByDefaultPolicy,
    resolve_recovery,
)

if TYPE_CHECKING:
    from pathlib import Path


def _plan(failure_class: str, *steps: RecoveryStep, **kwargs: object) -> RecoveryPlan:
    return RecoveryPlan(
        failure_class,  # type: ignore[arg-type]
        steps or (RecoveryStep.ESCALATE,),
        auto_retry_once=bool(kwargs.pop("auto_retry_once", False)),
        escalate=bool(kwargs.pop("escalate", True)),
        **kwargs,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------
# RecoveryRecipeBook
# ------------------------------------------------------------------


def test_default_book_matches_the_kernel_table() -> None:
    book = RecoveryRecipeBook.default()
    for failure_class in get_args(FailureClass):
        assert book.recommend(failure_class) == recommend_recovery(failure_class)


def test_declared_recipe_overrides_only_its_class() -> None:
    declared = _plan(
        "provider_rate_limit", RecoveryStep.BACKOFF_RETRY, RecoveryStep.ESCALATE,
        auto_retry_once=True,
    )
    book = RecoveryRecipeBook.default().declare(declared)

    assert book.recommend("provider_rate_limit") == declared
    assert book.recommend("provider_auth") == recommend_recovery("provider_auth")
    assert book.declared_classes == frozenset({"provider_rate_limit"})
    # 原表不受影响
    assert RecoveryRecipeBook.default().declared_classes == frozenset()


def test_unknown_failure_class_falls_back_to_the_unknown_recipe() -> None:
    book = RecoveryRecipeBook.default().declare(_plan("unknown", RecoveryStep.RETRY))
    assert book.recommend("never_heard_of").steps == (RecoveryStep.RETRY,)  # type: ignore[arg-type]


def test_custom_steps_are_serialized_only_when_present() -> None:
    plain = _plan("runtime_io")
    custom = _plan("provider_internal", RecoveryStep.BACKOFF_RETRY,
                   custom_steps=("switch_endpoint",))

    assert "custom_steps" not in plain.to_dict()
    assert custom.to_dict()["custom_steps"] == ["switch_endpoint"]


@pytest.mark.parametrize(
    "plan",
    [
        lambda: _plan("made_up_class"),
        lambda: RecoveryPlan("runtime_io", (), auto_retry_once=False, escalate=True),
        lambda: _plan("runtime_io", custom_steps=("",)),
        lambda: _plan("runtime_io", custom_steps=("retry",)),
        lambda: _plan("cancelled", RecoveryStep.RETRY, auto_retry_once=True),
    ],
    ids=["unknown-class", "no-steps", "blank-custom-step", "custom-step-shadows-kernel-step",
         "retry-on-cancelled"],
)
def test_invalid_declarations_are_rejected(plan: object) -> None:
    with pytest.raises(ValueError):
        RecoveryRecipeBook.default().declare(plan())  # type: ignore[operator]


def test_declaring_the_same_class_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="declared more than once"):
        RecoveryRecipeBook.default().declare(
            _plan("runtime_io"), _plan("runtime_io", RecoveryStep.RETRY)
        )


# ------------------------------------------------------------------
# policy 接入
# ------------------------------------------------------------------


def _ctx() -> FailureContext:
    return FailureContext(
        origin="llm_error", failure_class="provider_rate_limit", end_reason=None,
        error_kind="RateLimitError", retryable=True, is_root=True, iteration=1,
    )


def test_wrapper_keeps_the_disposition_of_the_wrapped_policy() -> None:
    book = RecoveryRecipeBook.default()
    assert RecipeDeclaringPolicy(ConservativeFailurePolicy(), book).decide(_ctx()) is (
        FailureDisposition.SUSPEND
    )
    terminal = FailureContext(
        origin="guard_trip", failure_class=None, end_reason="max_iterations",
        error_kind=None, retryable=False, is_root=True, iteration=3,
    )
    assert RecipeDeclaringPolicy(ConservativeFailurePolicy(), book).decide(terminal) is (
        FailureDisposition.TERMINAL
    )
    assert RecipeDeclaringPolicy(SuspendByDefaultPolicy(), book).decide(terminal) is (
        FailureDisposition.SUSPEND
    )


def test_wrapper_is_a_recipe_provider_but_plain_policies_are_not() -> None:
    wrapped = RecipeDeclaringPolicy(ConservativeFailurePolicy(), RecoveryRecipeBook.default())
    assert isinstance(wrapped, RecoveryRecipeProvider)
    assert not isinstance(ConservativeFailurePolicy(), RecoveryRecipeProvider)


def test_resolve_uses_kernel_table_without_a_provider() -> None:
    expected = recommend_recovery("provider_auth").to_dict()
    assert resolve_recovery(None, "provider_auth") == expected
    assert resolve_recovery(ConservativeFailurePolicy(), "provider_auth") == expected


def test_resolve_marks_declared_recipes() -> None:
    declared = _plan("provider_auth", RecoveryStep.CHECK_CREDENTIALS,
                     custom_steps=("rotate_key",))
    policy = RecipeDeclaringPolicy(
        ConservativeFailurePolicy(), RecoveryRecipeBook.default().declare(declared)
    )

    assert resolve_recovery(policy, "provider_auth") == {
        **declared.to_dict(), "source": "declared",
    }
    # 未声明的类别仍是内核配方，且不带 source
    assert resolve_recovery(policy, "runtime_io") == recommend_recovery("runtime_io").to_dict()


def test_provider_returning_a_plan_for_another_class_is_not_trusted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Confused:
        def decide(self, ctx: FailureContext) -> FailureDisposition:
            return FailureDisposition.TERMINAL

        def recovery_for(self, failure_class: str) -> RecoveryPlan | None:
            return recommend_recovery("cancelled")

    with caplog.at_level(logging.ERROR):
        resolved = resolve_recovery(_Confused(), "provider_auth")

    assert resolved == recommend_recovery("provider_auth").to_dict()
    assert any("provider_auth" in r.getMessage() for r in caplog.records)


def test_provider_raising_is_not_swallowed_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Broken:
        def decide(self, ctx: FailureContext) -> FailureDisposition:
            return FailureDisposition.TERMINAL

        def recovery_for(self, failure_class: str) -> RecoveryPlan | None:
            raise RuntimeError("recipe store down")

    with caplog.at_level(logging.ERROR):
        resolved = resolve_recovery(_Broken(), "runtime_io")

    # 失败处置路径上不能再抛：退回内核配方，但必须留下错误日志
    assert resolved == recommend_recovery("runtime_io").to_dict()
    assert any(r.exc_info for r in caplog.records)


# ------------------------------------------------------------------
# 引擎级：turn_failed 带声明的配方
# ------------------------------------------------------------------


async def test_turn_failed_carries_the_declared_recipe(
    skills_dir: Path, threads_dir: Path,
) -> None:
    declared = _plan(
        "content_filter", RecoveryStep.ADJUST_INPUT, custom_steps=("route_to_reviewer",),
        escalate=True,
    )
    policy = RecipeDeclaringPolicy(
        ConservativeFailurePolicy(), RecoveryRecipeBook.default().declare(declared)
    )

    class _Filtered(SimClient):
        """每次采样都被内容安全拦截。"""

        def _next_turn(self, request: object) -> SimTurn:
            raise ContentFilterError("blocked")

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir, threads_dir=threads_dir,
        model_client=_Filtered(turns=[SimTurn(text="不会到这里")]),
        compressors=[], failure_policy=policy,
    )
    engine = await pool.get_or_create(session_id="s", entry_skill_id="code-reviewer")
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    failed = None
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_failed", "turn_completed", "turn_suspended"):
            failed = ev.msg
            break

    assert failed is not None and failed.kind == "turn_failed"
    assert failed.data["failure_class"] == "content_filter"
    assert failed.data["recovery"] == {**declared.to_dict(), "source": "declared"}
    await pool.close()
