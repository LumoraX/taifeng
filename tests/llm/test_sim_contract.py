"""sim/contract.py 单测 —— 每条合同规则至少 1 正 1 反用例。"""

from __future__ import annotations

import pytest

from taifeng.llm.errors import LLMError
from taifeng.llm.providers.sim import SimContractViolation, SimTurn
from taifeng.llm.providers.sim.contract import RequestContractValidator
from taifeng.llm.types import ApiMessage, ApiRequest, ToolSpecRef


def _req(messages: list[ApiMessage], tools: list[ToolSpecRef] | None = None) -> ApiRequest:
    """构造最小 ApiRequest。"""
    return ApiRequest(model="sim-model", messages=messages, tools=tools or [])


def _fc(call_id: str, name: str = "read_skill") -> ApiMessage:
    """assistant 声明一个 tool_call（prompt.py 的 OpenAI 嵌套形状）。"""
    return ApiMessage(
        role="assistant",
        content="",
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }],
    )


def _out(call_id: str, output: str = "ok") -> ApiMessage:
    """tool 消息核销一个 call_id。"""
    return ApiMessage(role="tool", content=output, tool_call_id=call_id)


validator = RequestContractValidator()


# ---------------------------------------------------------------------------
# 正例：合法结构必须放行
# ---------------------------------------------------------------------------

def test_valid_plain_conversation_passes():
    """无工具调用的普通对话合法。"""
    validator.validate(_req([
        ApiMessage(role="user", content="你好"),
        ApiMessage(role="assistant", content="您好"),
        ApiMessage(role="user", content="继续"),
    ]))


def test_valid_paired_tool_call_passes():
    """声明 + 核销配对完整合法。"""
    validator.validate(_req([
        ApiMessage(role="user", content="查一下"),
        _fc("c1"),
        _out("c1"),
    ]))


def test_parallel_declare_then_interleaved_settle_passes():
    """并行 fan-out 真实形状：多条声明在前、输出乱序核销在后——必须合法。"""
    validator.validate(_req([
        ApiMessage(role="user", content="并发查"),
        _fc("c1"),
        _fc("c2"),
        _out("c2"),
        _out("c1"),
    ]))


def test_midstream_system_message_passes():
    """中段 system 消息（compacted 摘要 / system_injection）合法。"""
    validator.validate(_req([
        ApiMessage(role="user", content="hi"),
        ApiMessage(role="system", content="[Compacted history summary] ..."),
        _fc("c1"),
        ApiMessage(role="system", content="业务注记"),
        _out("c1"),
    ]))


# ---------------------------------------------------------------------------
# 反例：每条规则的违规形态
# ---------------------------------------------------------------------------

def test_empty_messages_rejected():
    with pytest.raises(SimContractViolation) as ei:
        validator.validate(_req([]))
    assert ei.value.rule == "empty_messages"


def test_unsettled_at_sampling_rejected():
    """末尾仍有未核销 call_id（工具结果未回传 / 重建漂移）。"""
    with pytest.raises(SimContractViolation) as ei:
        validator.validate(_req([
            ApiMessage(role="user", content="查"),
            _fc("c1"),
        ]))
    assert ei.value.rule == "unsettled_at_sampling"


def test_dangling_output_rejected():
    """tool 消息引用未声明的 call_id（悬空输出，resume 重放错位典型形态）。"""
    with pytest.raises(SimContractViolation) as ei:
        validator.validate(_req([
            ApiMessage(role="user", content="查"),
            _out("ghost"),
        ]))
    assert ei.value.rule == "dangling_output"


def test_duplicate_declaration_rejected():
    """同 call_id 重复声明（重放复读典型形态）。"""
    with pytest.raises(SimContractViolation) as ei:
        validator.validate(_req([
            ApiMessage(role="user", content="查"),
            _fc("c1"),
            _out("c1"),
            _fc("c1"),
            _out("c1"),
        ]))
    assert ei.value.rule == "duplicate_declaration"


def test_duplicate_settlement_rejected():
    """同 call_id 重复核销。"""
    with pytest.raises(SimContractViolation) as ei:
        validator.validate(_req([
            ApiMessage(role="user", content="查"),
            _fc("c1"),
            _out("c1"),
            _out("c1"),
        ]))
    assert ei.value.rule == "duplicate_settlement"


def test_user_while_pending_rejected():
    """未核销期间插入 user 消息（steering 应在迭代边界并入）。"""
    with pytest.raises(SimContractViolation) as ei:
        validator.validate(_req([
            ApiMessage(role="user", content="查"),
            _fc("c1"),
            ApiMessage(role="user", content="插队"),
            _out("c1"),
        ]))
    assert ei.value.rule == "user_while_pending"


# ---------------------------------------------------------------------------
# 响应侧反查（task 2.2）
# ---------------------------------------------------------------------------

def _tool(name: str) -> ToolSpecRef:
    return ToolSpecRef(name=name, description="d", input_schema={})


def test_response_side_known_tool_passes():
    turn = SimTurn(tool_calls=[{"id": "c1", "name": "read_skill", "arguments": "{}"}])
    validator.validate_response_side(
        turn, _req([ApiMessage(role="user", content="x")], tools=[_tool("read_skill")])
    )


def test_response_side_unknown_tool_rejected():
    """脚本要调的工具没注册进请求 tools —— 抓 engine 漏注册类 bug。"""
    turn = SimTurn(tool_calls=[{"id": "c1", "name": "call_skill", "arguments": "{}"}])
    with pytest.raises(SimContractViolation) as ei:
        validator.validate_response_side(
            turn, _req([ApiMessage(role="user", content="x")], tools=[_tool("read_skill")])
        )
    assert ei.value.rule == "unknown_tool_response"


def test_violation_not_llm_error():
    """SimContractViolation 不得入 LLMError 体系（否则被 retry 消化测试红不了）。"""
    assert not issubclass(SimContractViolation, LLMError)
