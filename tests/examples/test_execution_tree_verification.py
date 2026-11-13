"""验证执行观测场景必须具备双子节点归属、重叠执行与独立用量证据。"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from typing import Any

_ROOT = Path(__file__).resolve().parents[2]


def _modules(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """加载场景且禁止 dotenv，确保测试只有 sim 可见请求。"""
    monkeypatch.syspath_prepend(str(_ROOT / "examples"))
    monkeypatch.syspath_prepend(str(_ROOT / "examples" / "real_llm"))
    bootstrap = importlib.import_module("_provider_bootstrap")
    monkeypatch.setattr(bootstrap, "load_dotenv_files", lambda *paths: [])
    assert importlib.util.find_spec("_execution_tree") is not None, "缺少执行树取证场景"
    return importlib.import_module("_execution_tree"), importlib.import_module("capability_matrix")


@pytest_asyncio.fixture
async def verified_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """真实内核驱动两个同 skill 子执行，模拟器只读取可见请求。"""
    module, matrix = _modules(monkeypatch)
    scenario = next(sc for sc in matrix.SCENARIOS if sc.demo_id == "execution_observability")
    client = module.ExecutionTreeSimClient()
    result = await matrix.run_scenario(client, scenario, tmp_path / "logs")
    assert matrix._verdict(result)[0] == "✅PASS"
    assert not client.ledger.violations
    return module, result


async def test_same_skill_children_overlap_with_separate_usage(verified_run: tuple[Any, Any]) -> None:
    """三个 attempt 的自身用量相加必须恰好等于会话账，不能重复加子树。"""
    module, result = verified_run
    proof = module.verify_execution_evidence(result)
    assert len(proof["children"]) == 2
    assert proof["own_tokens"] < proof["session_tokens"]
    assert proof["session_tokens"] == result.state["introspection"]["usage"]["total_tokens"]


def _damage_metadata(envelope: Any, damage: str, root_identity: Any) -> Any:
    """分别破坏身份与所属关系，保持正文证据不变。"""
    identity, message = envelope.execution, envelope.msg
    child = identity is not None and identity.execution_id != root_identity.execution_id
    changes: dict[str, Any] = {}
    if damage == "sequence" and child:
        changes["seq"] = envelope.seq + 1
    elif damage == "identity" and child:
        changes["execution"] = None
    elif damage == "parent" and child:
        changes["execution"] = identity.model_copy(update={"parent_execution_id": "wrong"})
    elif damage == "submission" and child:
        changes["submission_id"] = "other-submission"
    elif damage in {"tool_owner", "text_owner"} and child and message.kind == (
        "tool_call_completed" if damage == "tool_owner" else "assistant_text"
    ):
        changes["execution"] = root_identity
    return envelope.model_copy(update=changes)


def _damage_content(message: Any, damage: str, root: str) -> Any:
    """分别破坏用量、上下文、并行配置和请求，避免一个变体修复另一个。"""
    data = dict(message.data)
    if damage == "usage" and message.kind == "execution_updated":
        data["usage"] = {**data["usage"], "total_tokens": 999999}
    elif damage == "context" and message.kind == "execution_updated":
        data["context_measured"] = "unknown"
    elif damage == "context_value" and message.kind == "execution_updated":
        data["context_tokens"] = 123456
    elif damage == "batch" and message.kind == "tool_batch_dispatched":
        data["max_parallel"] = 1
    elif damage == "request_leak" and message.kind == "llm_request_recorded":
        data["execution_id"] = root
    return message.model_copy(update={"data": data})


@pytest.mark.parametrize("damage", [
    "sequence", "identity", "parent", "tool_owner", "text_owner", "usage", "context", "overlap",
    "batch", "submission", "request_leak", "root_terminal", "failure", "context_value",
])
async def test_execution_verifier_rejects_damaged_evidence(
    verified_run: tuple[Any, Any], damage: str,
) -> None:
    """移除或混淆每种关键事实都应使纯验证函数拒绝运行。"""
    from taifeng.loop.event import TurnFailed

    module, result = verified_run
    root = result.state["root_thread_id"]
    root_identity = next(ev.execution for ev in result.envelopes
                         if ev.execution is not None and ev.execution.execution_id == root)
    if damage == "overlap":
        result.state["probes"][1]["entered"] = result.state["probes"][0]["exited"] + 1
    else:
        for index, envelope in enumerate(result.envelopes):
            changed = _damage_metadata(envelope, damage, root_identity)
            result.envelopes[index] = changed.model_copy(update={
                "msg": _damage_content(envelope.msg, damage, root)})
        if damage == "root_terminal":
            result.envelopes = [ev for ev in result.envelopes
                                if not (ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root"))]
        elif damage == "failure":
            event = result.envelopes[-1]
            result.envelopes.append(event.model_copy(update={"msg": TurnFailed(data={})}))
    with pytest.raises(AssertionError):
        module.verify_execution_evidence(result)


@pytest.mark.parametrize(("usage", "measured"), [
    ({}, False),
    ({"output_tokens": 5}, False),
    ({"input_tokens": True, "output_tokens": 5}, False),
    ({"input_tokens": -1, "output_tokens": 5}, False),
    ({"input_tokens": 42}, True),
    ({"input_tokens": 42, "output_tokens": True}, True),
    ({"input_tokens": 42, "output_tokens": -1}, True),
])
async def test_incomplete_usage_keeps_context_measurement_honest(
    usage: dict[str, Any], measured: bool,
) -> None:
    """输入实测与总用量完整度独立：缺输出不能抹去已知输入，缺输入必须标估算。"""
    from types import SimpleNamespace

    from taifeng.llm.types import TokenUsage
    from taifeng.loop.execution_identity import ExecutionIdentity
    from taifeng.loop.execution_observer import emit_sample_observation

    emitted = []

    async def emit(event: Any) -> None:
        """只截获真实观察函数发出的信封，不模拟其计量逻辑。"""
        emitted.append(event)

    owner = SimpleNamespace(
        _execution_identity=ExecutionIdentity(
            execution_id="thread", thread_id="thread", attempt_id="attempt", skill_id="worker"),
        _execution_usage_complete=True,
        _sample=SimpleNamespace(samples_started=1, samples_metered=1),
        _history_token_estimate=lambda: 73,
        effective_budget=SimpleNamespace(context_window=8192),
        total_usage=TokenUsage(), submission_id="submission", emit=emit,
    )
    await emit_sample_observation(owner, 1, usage=usage)
    assert len(emitted) == 1
    data = emitted[0].msg.data
    assert data["context_measured"] is measured
    assert data["context_tokens"] == (42 if measured else 73)
    assert data["usage_complete"] is False


async def test_usage_completeness_reports_attempt_and_missing_counts() -> None:
    """执行观测报告 provider 尝试、计量和缺失用量次数。"""
    from types import SimpleNamespace

    from taifeng.llm.types import TokenUsage
    from taifeng.loop.execution_identity import ExecutionIdentity
    from taifeng.loop.execution_observer import emit_sample_observation

    emitted = []

    async def emit(event: Any) -> None:
        """捕获执行观测事件。"""
        emitted.append(event)

    owner = SimpleNamespace(
        _execution_identity=ExecutionIdentity(
            execution_id="thread", thread_id="thread", attempt_id="attempt", skill_id="worker"),
        _execution_usage_complete=False,
        _sample=SimpleNamespace(samples_started=3, samples_metered=2),
        _history_token_estimate=lambda: 73,
        effective_budget=SimpleNamespace(context_window=8192),
        total_usage=TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
        submission_id="submission", emit=emit,
    )
    await emit_sample_observation(owner, 2, usage={"input_tokens": 4, "output_tokens": 2})
    data = emitted[-1].msg.data
    assert data["usage_complete"] is False
    assert data["usage_attempts_started"] == 3
    assert data["usage_attempts_metered"] == 2
    assert data["usage_attempts_missing"] == 1
