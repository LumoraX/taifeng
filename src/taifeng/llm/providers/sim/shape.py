"""形状签名抽取器 —— sim 与真实 provider 事件流「形状」定义的单一真相。

金样校准（llm-golden-calibration）核心件：录制端（examples/real_llm/_recorder.py）
与校验端（tests/llm/test_golden_calibration.py）共用本模块，把一次 sampling 的
``ResponseEvent`` 流确定性归约为 :class:`ShapeSignature`。

设计要点（见 openspec change llm-golden-calibration design D3）：

- **零文本零数值**：签名只含事件 kind、字段名与值类型类别——不存原始文本 /
  具体 token 数 / model 名 / API 凭据，脱敏是结构性保证而非后处理。
- **delta 段折叠**：连续 delta 类事件（text / reasoning / tool_call delta，可混 kind）
  折叠为单项并记录该段 kind 集合——真实 provider 的分块数与交错顺序随机，
  按段折叠让同一形状的多次录制产出同一签名（免 flaky）。
- **环境噪声只录不比**：``rate_limits`` 出现与否取决于网关 HTTP 头（环境因素，
  非响应语义），不进 kind 序列；delta 块数同理只记入 chunking 供人工 review。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng.llm.events import ResponseEvent

# delta 类 kind：连续段（可混 kind）折叠为单项
_DELTA_KINDS = frozenset({"text_delta", "reasoning_delta", "tool_call_delta"})

# 环境噪声 kind：出现与否取决于网关响应头等环境因素 → 不进 kind 序列，只录不比
_ENV_KINDS = frozenset({"rate_limits"})

# 协议定义的稳定嵌套结构：这些字段的 dict 值展开一层校验子字段。
# 其余 dict 值（如 structured_output.parsed 是业务载荷，内容随场景变化）只校类型类别。
_EXPAND_DICT_FIELDS = frozenset({"usage"})


def _type_class(value: object) -> str:
    """值 → 类型类别字符串（不含具体值；bool 必须先于 int 判定）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    # 协议层事件 data 只应出现以上 JSON 基本类型；其余类型按类名记录（漂移会被比对暴露）
    return type(value).__name__


@dataclass(frozen=True)
class ShapeSignature:
    """一次 sampling 事件流的形状签名。

    比对维度（``comparable()`` 返回）：kind_sequence / terminal / field_shapes / presence。
    只录不比维度：chunking（delta 块数）、observed_env（环境噪声 kind 出现与否）。
    """

    # 折叠后的事件 kind 序列；delta 段表示为 "delta[kind1,kind2]"（kind 集合排序拼接）
    kind_sequence: tuple[str, ...]
    # 终止事件类别："completed" / "error" / "truncated"（流半途断开，无终止事件）
    terminal: str
    # 字段结构：("kind.field" 或 "kind.field.subfield", 类型类别并集如 "null|str")，排序
    field_shapes: tuple[tuple[str, str], ...]
    # presence 标志：(名, 出现与否)，排序——reasoning / tool_calls / structured 等
    presence: tuple[tuple[str, bool], ...]
    # 只录不比：各 delta 类 kind 的总块数（供人工 review provider 分块行为）
    chunking: tuple[tuple[str, int], ...] = field(default=())
    # 只录不比：环境噪声 kind 的出现与否（如 rate_limits）
    observed_env: tuple[tuple[str, bool], ...] = field(default=())

    def comparable(self) -> dict[str, Any]:
        """返回参与比对的维度（chunking / observed_env 不进比对——D3 裁决）。"""
        return {
            "kind_sequence": list(self.kind_sequence),
            "terminal": self.terminal,
            "field_shapes": [list(p) for p in self.field_shapes],
            "presence": [list(p) for p in self.presence],
        }

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容 dict（金样 JSONL 落盘用）。"""
        return {
            "kind_sequence": list(self.kind_sequence),
            "terminal": self.terminal,
            "field_shapes": [list(p) for p in self.field_shapes],
            "presence": [list(p) for p in self.presence],
            "chunking": [list(p) for p in self.chunking],
            "observed_env": [list(p) for p in self.observed_env],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShapeSignature:
        """从金样 JSONL 行反序列化（字段缺失即抛 KeyError——禁 silent fallback）。"""
        return cls(
            kind_sequence=tuple(data["kind_sequence"]),
            terminal=data["terminal"],
            field_shapes=tuple((str(k), str(v)) for k, v in data["field_shapes"]),
            presence=tuple((str(k), bool(v)) for k, v in data["presence"]),
            chunking=tuple((str(k), int(v)) for k, v in data["chunking"]),
            observed_env=tuple((str(k), bool(v)) for k, v in data["observed_env"]),
        )


def shape_class_key(sig: ShapeSignature) -> str:
    """签名 → 形状类别 key（出现的基础 kind 集合 + 终止类别）。

    校准测试按类别去重金样、按类别查 SimTurn builder；delta 折叠项展开回成员 kind。
    """
    kinds: set[str] = set()
    for item in sig.kind_sequence:
        if item.startswith("delta["):
            kinds.update(item[len("delta[") : -1].split(","))
        else:
            kinds.add(item)
    return "+".join(sorted(kinds)) + f"->{sig.terminal}"


def extract_shape(events: Sequence[ResponseEvent]) -> ShapeSignature:
    """事件流 → 形状签名（确定性：同流两次调用结果相等，无时间/随机量混入）。"""
    kind_sequence: list[str] = []
    delta_run: set[str] = set()  # 当前连续 delta 段已出现的 kind 集合
    field_types: dict[str, set[str]] = {}  # "kind.field" → 观测到的类型类别集合
    chunk_counts: dict[str, int] = {}
    env_seen: dict[str, bool] = dict.fromkeys(sorted(_ENV_KINDS), False)

    def _flush_delta_run() -> None:
        """连续 delta 段结束 → 折叠为单项写入序列。"""
        if delta_run:
            kind_sequence.append(f"delta[{','.join(sorted(delta_run))}]")
            delta_run.clear()

    for ev in events:
        if ev.kind in _ENV_KINDS:
            env_seen[ev.kind] = True
            continue  # 环境噪声不进序列、不进字段结构
        if ev.kind in _DELTA_KINDS:
            delta_run.add(ev.kind)
            chunk_counts[ev.kind] = chunk_counts.get(ev.kind, 0) + 1
        else:
            _flush_delta_run()
            kind_sequence.append(ev.kind)
        _collect_field_shapes(ev.kind, ev.data, field_types)
    _flush_delta_run()

    terminal = "truncated"
    if events and events[-1].kind in ("completed", "error"):
        terminal = events[-1].kind

    presence = _build_presence(events, field_types)
    return ShapeSignature(
        kind_sequence=tuple(kind_sequence),
        terminal=terminal,
        field_shapes=tuple(
            (key, "|".join(sorted(types))) for key, types in sorted(field_types.items())
        ),
        presence=presence,
        chunking=tuple(sorted(chunk_counts.items())),
        observed_env=tuple(sorted(env_seen.items())),
    )


def _collect_field_shapes(
    kind: str, data: dict[str, Any], field_types: dict[str, set[str]]
) -> None:
    """收集单事件 data 的字段名 + 类型类别；协议嵌套结构（usage）展开一层。"""
    for name, value in data.items():
        if name in _EXPAND_DICT_FIELDS and isinstance(value, dict):
            for sub, sub_value in value.items():
                field_types.setdefault(f"{kind}.{name}.{sub}", set()).add(_type_class(sub_value))
        else:
            field_types.setdefault(f"{kind}.{name}", set()).add(_type_class(value))


def _build_presence(
    events: Sequence[ResponseEvent], field_types: dict[str, set[str]]
) -> tuple[tuple[str, bool], ...]:
    """presence 标志：各语义成分出现与否 + 终态字段非空性（排序保证确定性）。"""
    kinds = {ev.kind for ev in events}
    flags = {
        "has_reasoning": "reasoning_delta" in kinds,
        "has_text": "text_delta" in kinds,
        "has_tool_calls": "tool_call_done" in kinds,
        "has_structured": "structured_output" in kinds,
        "has_prompt_cache": "prompt_cache" in kinds,
        # 只记存在性不记值（D3：request_id / end_turn 值不进签名）
        "request_id_present": field_types.get("completed.request_id") == {"str"},
        "end_turn_present": "completed.end_turn" in field_types
        and field_types["completed.end_turn"] != {"null"},
    }
    return tuple(sorted(flags.items()))
