"""金样校准测试 —— sim 事件流形状必须与真实 provider 录制的金样一致（漂移红线）。

工作机制（llm-golden-calibration design D4/D6）：

1. 加载 ``tests/llm/golden/*.jsonl``（由 ``capability_matrix.py --record`` 用真实 key
   录制，CI 内离线读取，零网络调用）；
2. 对每条去重金样签名，按其特征（has_text / has_reasoning / tool 数 / structured /
   request_id 存在性）**参数化构造** SimTurn 脚本，跑 SimClient 抽取 sim 签名；
3. 逐比对维度断言一致；金样中出现 sim 表达不了的形状类别 → 测试红并打印类别 key。

失败处方（D6）：禁止自动放宽比对维度——要么重录金样并人工 review diff，
要么给 sim 补合成能力 / builder 补项（走 PR review）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taifeng.llm.providers.sim import SimClient, SimTurn
from taifeng.llm.providers.sim.shape import (
    ShapeSignature,
    extract_shape,
    shape_class_key,
)
from taifeng.llm.types import ApiRequest, ResponseFormatSpec, ToolSpecRef
from taifeng.loop.cancellation import CancellationToken

GOLDEN_DIR = Path(__file__).parent / "golden"

# 校准探针用的强类型输出 schema（仅驱动 sim 的 structured_output 回放路径）
_CAL_FORMAT = ResponseFormatSpec(name="calibration", json_schema={"type": "object"})

RE_RECORD_CMD = (
    "PYTHONPATH=src uv run python examples/real_llm/capability_matrix.py --record"
)
PRESCRIPTION = (
    f"\n处方：①若真实 provider 行为已变 → 重录金样（{RE_RECORD_CMD}）并人工 review "
    "金样 diff 后提交；②若 sim 形状落后 → 给 sim 补合成能力 / builder 补项（走 PR review）。"
    "禁止放宽比对维度（design D6）。"
)


class UnsupportedShapeClass(AssertionError):  # noqa: N818 —— 语义是形状类别名词，仓库先例 SuspendSignal
    """金样中出现 sim 表达不了的形状类别 —— 漂移警报本体（design D4）。"""


def _load_golden(path: Path) -> list[ShapeSignature]:
    """读单场景金样 JSONL → 按比对维度去重的签名列表（保持观测顺序）。"""
    sigs: list[ShapeSignature] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sig = ShapeSignature.from_dict(json.loads(line)["signature"])
        key = json.dumps(sig.comparable(), sort_keys=True)
        if key not in seen:
            seen.add(key)
            sigs.append(sig)
    return sigs


def _build_turn_and_request(sig: ShapeSignature) -> tuple[SimTurn, ApiRequest]:
    """按金样签名特征参数化构造 SimTurn 脚本与采样请求。

    支持的形状成分：text / reasoning / N 个 tool_call / structured_output /
    request_id 存在性，终止类别 completed。出现 sim 表达不了的成分
    （error 终态、未知 kind）→ 抛 :class:`UnsupportedShapeClass`。
    """
    if sig.terminal != "completed":
        raise UnsupportedShapeClass(
            f"金样形状类别 {shape_class_key(sig)!r} 的终止类别为 {sig.terminal!r}，"
            f"sim 目前只能合成 completed 终态的事件流。{PRESCRIPTION}"
        )
    known = {"created", "server_model", "tool_call_done", "structured_output",
             "prompt_cache", "completed", "text_delta", "reasoning_delta",
             "tool_call_delta"}
    kinds: set[str] = set()
    for item in sig.kind_sequence:
        if item.startswith("delta["):
            kinds.update(item[len("delta[") : -1].split(","))
        else:
            kinds.add(item)
    unknown = kinds - known
    if unknown:
        raise UnsupportedShapeClass(
            f"金样形状类别 {shape_class_key(sig)!r} 含 sim 从不合成的事件 kind "
            f"{sorted(unknown)}。{PRESCRIPTION}"
        )

    presence = dict(sig.presence)
    n_tools = sum(1 for item in sig.kind_sequence if item == "tool_call_done")
    turn = SimTurn(
        text="校准用文本回放，长度跨多个切片以驱动流式分块。" if presence["has_text"] else "",
        reasoning="校准用思考轨迹回放。" if presence["has_reasoning"] else "",
        tool_calls=[
            {"id": f"cal-{i}", "name": "calibration_tool",
             "arguments": json.dumps({"index": i})}
            for i in range(n_tools)
        ],
        structured={"calibration": True} if presence["has_structured"] else None,
        request_id="req-cal" if presence["request_id_present"] else None,
    )
    request = ApiRequest(
        model="sim-model",
        messages=[{"role": "user", "content": "calibration probe"}],
        tools=[ToolSpecRef(name="calibration_tool", description="校准探针工具",
                           input_schema={})] if n_tools else [],
        response_format=_CAL_FORMAT if presence["has_structured"] else None,
    )
    return turn, request


async def _sim_signature(turn: SimTurn, request: ApiRequest) -> ShapeSignature:
    """跑一次 SimClient 采样，收集完整事件流并抽取签名。"""
    client = SimClient(turns=[turn])
    events = []
    async with client.session(cancel=CancellationToken()) as sess:
        async for ev in sess.stream(request):
            events.append(ev)
    return extract_shape(events)


def _assert_shape_match(golden: ShapeSignature, sim: ShapeSignature, source: str) -> None:
    """逐比对维度断言一致；失败信息含逐维 diff + 重录处方（无任何宽松开关）。"""
    g, s = golden.comparable(), sim.comparable()
    if g == s:
        return
    diff_lines = [f"金样形状漂移：{source}（类别 {shape_class_key(golden)!r}）"]
    for dim in g:
        if g[dim] != s[dim]:
            diff_lines.append(f"  [{dim}]\n    golden: {g[dim]}\n    sim:    {s[dim]}")
    raise AssertionError("\n".join(diff_lines) + PRESCRIPTION)


def _golden_files() -> list[Path]:
    """金样文件清单（CI 离线读取）。"""
    return sorted(GOLDEN_DIR.glob("*.jsonl")) if GOLDEN_DIR.is_dir() else []


@pytest.mark.parametrize(
    "golden_path",
    _golden_files() or [None],
    ids=lambda p: p.stem if isinstance(p, Path) else "no-golden",
)
async def test_sim_calibrated_against_golden(golden_path: Path | None) -> None:
    """核心校准：每条去重金样签名都能被 sim 以同形状复现。"""
    if golden_path is None:
        pytest.skip(f"无金样 fixture：先用真实 key 录制 —— {RE_RECORD_CMD}")
    for i, golden_sig in enumerate(_load_golden(golden_path)):
        turn, request = _build_turn_and_request(golden_sig)
        sim_sig = await _sim_signature(turn, request)
        _assert_shape_match(golden_sig, sim_sig, source=f"{golden_path.name}#{i}")


# ── harness 自洽性（不依赖金样存在，钉死校准机制本身）──────────────────────


@pytest.mark.parametrize(
    ("text", "reasoning", "n_tools", "structured"),
    [
        (True, False, 0, False),   # 纯文本完成
        (True, True, 0, False),    # reasoning + 文本
        (True, False, 2, False),   # 文本 + 双工具调用
        (True, False, 0, True),    # structured output
        (False, False, 1, False),  # 纯工具调用
    ],
)
async def test_builder_self_consistency(
    text: bool, reasoning: bool, n_tools: int, structured: bool
) -> None:
    """自洽性：sim 流的签名经参数化 builder 重建后能精确复现自身（机制无金样也被验证）。"""
    seed_turn = SimTurn(
        text="种子文本内容一段。" if text else "",
        reasoning="种子思考。" if reasoning else "",
        tool_calls=[
            {"id": f"s{i}", "name": "calibration_tool", "arguments": '{"a": 1}'}
            for i in range(n_tools)
        ],
        structured={"x": 1} if structured else None,
        request_id="req-seed",
    )
    seed_request = ApiRequest(
        model="sim-model",
        messages=[{"role": "user", "content": "seed"}],
        tools=[ToolSpecRef(name="calibration_tool", description="d",
                           input_schema={})] if n_tools else [],
        response_format=_CAL_FORMAT if structured else None,
    )
    seed_sig = await _sim_signature(seed_turn, seed_request)
    rebuilt_turn, rebuilt_request = _build_turn_and_request(seed_sig)
    rebuilt_sig = await _sim_signature(rebuilt_turn, rebuilt_request)
    _assert_shape_match(seed_sig, rebuilt_sig, source="self-consistency")


async def test_tampered_golden_goes_red_with_prescription() -> None:
    """漂移红线验收：篡改金样任一比对维度 → 红测且报错含逐维 diff 与重录处方。"""
    seed_sig = await _sim_signature(
        SimTurn(text="篡改实验文本。"),
        ApiRequest(model="sim-model", messages=[{"role": "user", "content": "t"}]),
    )
    # 模拟 provider 漂移：completed.usage 多了一个 sim 不产出的新字段
    tampered = ShapeSignature.from_dict(
        {
            **seed_sig.to_dict(),
            "field_shapes": [*seed_sig.to_dict()["field_shapes"],
                             ["completed.usage.brand_new_field", "int"]],
        }
    )
    turn, request = _build_turn_and_request(tampered)
    sim_sig = await _sim_signature(turn, request)
    with pytest.raises(AssertionError, match="重录金样") as excinfo:
        _assert_shape_match(tampered, sim_sig, source="tamper-test")
    assert "field_shapes" in str(excinfo.value)
    assert "brand_new_field" in str(excinfo.value)


async def test_unsupported_class_goes_red_with_class_key() -> None:
    """金样含 sim 表达不了的类别（error 终态 / 未知 kind）→ 红测并打印类别 key。"""
    error_terminal = ShapeSignature(
        kind_sequence=("created", "error"),
        terminal="error",
        field_shapes=(("error.kind", "str"),),
        presence=(
            ("end_turn_present", False), ("has_prompt_cache", False),
            ("has_reasoning", False), ("has_structured", False),
            ("has_text", False), ("has_tool_calls", False),
            ("request_id_present", False),
        ),
    )
    with pytest.raises(UnsupportedShapeClass, match="error"):
        _build_turn_and_request(error_terminal)

    unknown_kind = ShapeSignature(
        kind_sequence=("created", "mystery_event", "completed"),
        terminal="completed",
        field_shapes=(),
        presence=error_terminal.presence,
    )
    with pytest.raises(UnsupportedShapeClass, match="mystery_event"):
        _build_turn_and_request(unknown_kind)


def test_comparator_has_no_loosening_knobs() -> None:
    """D6 复核：比对函数签名不暴露任何忽略维度 / 宽松模式参数。"""
    import inspect

    params = inspect.signature(_assert_shape_match).parameters
    assert set(params) == {"golden", "sim", "source"}
