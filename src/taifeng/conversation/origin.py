"""输入来源标记 —— 进入模型视野的内容是谁给的、可不可信（input-origin，ADR 0085）。

模型看到的内容来源各异：用户亲手输入、宿主程序注入、工具从外部取回、其他 agent 发来。
来自外部的内容可能夹带指令（prompt injection）。内核不判断内容是否恶意，也不决定该怎么
防——它只做两件事：

1. **记下来源**：条目的 ``metadata["origin"]`` 记录 ``{kind, trust, label?}``，由把内容送进来
   的一方声明（业务提交 Op 时、工具在 ``ToolSpec.output_trust`` 上），内核派生的内容
   （压缩摘要、子 skill 的种子消息）继承其来源的不可信标记；
2. **汇总透出**：``summarize_taint`` 给出当前上下文里不可信内容的汇总，经
   ``ToolContext.extras["input_taint"]`` 与 hook 上下文交给业务。要不要在上下文被污染时
   拦截某个工具、改为询问，由业务的 hook / 权限策略决定（R1）。

标记只在 metadata 里，不进 prompt：模型看到的内容与是否打标无关（R2）。

没有标记的条目视为「未声明」：既不算可信也不算不可信，不计入污染汇总。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from taifeng.conversation.models import ResponseItem

ORIGIN_METADATA_KEY = "origin"
"""条目 metadata 里承载来源标记的键。"""

OriginKind = Literal["user", "host", "tool", "peer", "derived"]
"""谁把内容送进来的：用户 / 宿主程序 / 工具 / 其他 agent / 内核由已有内容派生。"""

OriginTrust = Literal["trusted", "untrusted"]
"""声明的可信度。"""


class InputOrigin(BaseModel):
    """一段输入的来源标记。

    Attributes:
        kind: 来源类别。
        trust: 声明的可信度。
        label: 业务自定义的不透明标签（如渠道名、工具名）；内核不解释，只原样汇总。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OriginKind
    trust: OriginTrust
    label: str | None = Field(default=None, min_length=1, max_length=128)

    def to_metadata(self) -> dict[str, str]:
        """落进条目 metadata 的形状；没有 label 时不带该键。"""
        data: dict[str, str] = {"kind": self.kind, "trust": self.trust}
        if self.label is not None:
            data["label"] = self.label
        return data


def tag_origin(item: ResponseItem, origin: InputOrigin | None) -> ResponseItem:
    """给条目打上来源标记，返回新条目；``origin`` 为 None 时原样返回。"""
    if origin is None:
        return item
    metadata = {**item.metadata, ORIGIN_METADATA_KEY: origin.to_metadata()}
    return item.model_copy(update={"metadata": metadata})


def origin_of(item: ResponseItem) -> InputOrigin | None:
    """读出条目的来源标记；没有标记返回 None。

    Raises:
        ValueError: metadata 里有 ``origin`` 键但形状不合法（数据损坏，不猜）。
    """
    raw = item.metadata.get(ORIGIN_METADATA_KEY)
    if raw is None:
        return None
    try:
        return InputOrigin.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"malformed origin metadata on item {item.id}") from exc


@dataclass(frozen=True)
class InputTaint:
    """上下文里不可信内容的汇总。

    Attributes:
        untrusted: 是否存在声明为不可信的内容。
        kinds: 不可信内容的来源类别（去重、排序）。
        labels: 不可信内容的标签（去重、排序）；没有标签的条目不贡献。
        item_count: 不可信条目数。
    """

    untrusted: bool = False
    kinds: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    item_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """交给工具 / hook 的 JSON 友好视图。"""
        return {
            "untrusted": self.untrusted,
            "kinds": list(self.kinds),
            "labels": list(self.labels),
            "item_count": self.item_count,
        }

    def derived_origin(self) -> InputOrigin | None:
        """由这些内容派生出的新内容应带的来源标记；没有不可信内容返回 None。

        标签拼接后超长时截断——标签是给人和策略看的线索，不是完整清单。
        """
        if not self.untrusted:
            return None
        label = ",".join(self.labels)[:128] or None
        return InputOrigin(kind="derived", trust="untrusted", label=label)


def summarize_taint(history: Sequence[ResponseItem]) -> InputTaint:
    """汇总一段 history 里声明为不可信的内容。

    派生标记（``kind="derived"``）的标签是逗号拼接的来源标签，汇总时拆回各自的标签。

    Raises:
        ValueError: 某条目的来源标记形状不合法。
    """
    kinds: set[str] = set()
    labels: set[str] = set()
    count = 0
    for item in history:
        origin = origin_of(item)
        if origin is None or origin.trust != "untrusted":
            continue
        count += 1
        kinds.add(origin.kind)
        if origin.label is not None:
            parts = origin.label.split(",") if origin.kind == "derived" else [origin.label]
            labels.update(part for part in parts if part)
    return InputTaint(
        untrusted=count > 0,
        kinds=tuple(sorted(kinds)),
        labels=tuple(sorted(labels)),
        item_count=count,
    )


def lost_taint(
    before: Sequence[ResponseItem], after: Sequence[ResponseItem],
) -> InputOrigin | None:
    """一次改写（如压缩）从 history 里拿走的不可信内容应由产物继承的来源标记。

    只看被拿走的条目（按条目 id 判断）；没有拿走不可信条目返回 None。
    """
    kept = {item.id for item in after}
    removed = [item for item in before if item.id not in kept]
    return summarize_taint(removed).derived_origin()


INPUT_TAINT_EXTRAS_KEY = "input_taint"
"""``ToolContext.extras`` / ``HookContext.extras`` 里承载污染汇总的键。"""


def taint_from_extras(extras: Mapping[str, Any]) -> InputTaint:
    """从工具 / hook 上下文的 extras 里还原污染汇总；没有该键视为干净的上下文。

    Raises:
        ValueError: 键存在但形状不合法。
    """
    raw = extras.get(INPUT_TAINT_EXTRAS_KEY)
    if raw is None:
        return InputTaint()
    try:
        return InputTaint(
            untrusted=bool(raw["untrusted"]),
            kinds=tuple(str(kind) for kind in raw["kinds"]),
            labels=tuple(str(label) for label in raw["labels"]),
            item_count=int(raw["item_count"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed input_taint in context extras") from exc


__all__ = [
    "INPUT_TAINT_EXTRAS_KEY",
    "ORIGIN_METADATA_KEY",
    "InputOrigin",
    "InputTaint",
    "OriginKind",
    "OriginTrust",
    "lost_taint",
    "origin_of",
    "summarize_taint",
    "tag_origin",
    "taint_from_extras",
]
