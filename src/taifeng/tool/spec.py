"""ToolSpec —— 工具描述与协议。

参照：codex codex-rs/tools/src/tool_spec.rs
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from taifeng.llm.types import ToolSpecRef

if TYPE_CHECKING:
    from taifeng.loop.cancellation import CancellationToken


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。"""

    output: str
    """LLM 可见的结果字符串（通常是 JSON 序列化或纯文本）。"""

    is_error: bool = False
    """是否为错误结果。"""

    data: dict[str, Any] = field(default_factory=dict)
    """额外元数据，不进 LLM 视野，供 telemetry 使用。"""

    @classmethod
    def ok(cls, output: str, **data: Any) -> ToolResult:
        return cls(output=output, is_error=False, data=data)

    @classmethod
    def error(cls, message: str, **data: Any) -> ToolResult:
        return cls(output=message, is_error=True, data=data)


@dataclass(frozen=True)
class ToolContext:
    """工具执行上下文 —— 注入到 ``ToolHandler.execute``。"""

    call_id: str
    cancel: CancellationToken
    thread_id: str
    # 业务层注入的附加引用（如 skill_snapshot / dispatch_policy / message_store）
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ToolHandler(Protocol):
    """工具执行函数协议。"""

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...


ToolFunc = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    """工具完整描述 —— LLM 可见 schema + 本地 handler。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolFunc
    parallel_safe: bool = False
    """True → 读锁（可并行）；False → 写锁（独占）。

    保守原则：写文件 / 调 LLM / 调子 skill 都用 False。"""

    timeout_seconds: float = 60.0
    """单次执行超时。超时后 cancel 触发 + 抛 TimeoutError。"""

    refunds_iteration: bool = False
    """True → 该工具调用**成功**后向外层 turn 迭代预算退还一步（不耗 max_iterations）。

    用于「内部批量调用」类内置工具（对标 hermes execute_code 的 refund 语义）。
    仅 spec 静态声明 + 内核 dispatch 路径生效，不暴露为 LLM 可触发语义；
    失败轮照常计费。本内核不为任何既有内置工具默认开启（使用方决策）。"""

    # === strict audit metadata（ADR 0025）===
    # 仅 strict audit 模式静态读取（AuditConfig 校验 + Journal tool_intent 落账）。
    # 默认取最安全的 pure/none/无幂等键/不可挂起 —— 无副作用只读工具开箱即用；
    # 有副作用的内置工具在各自 builtins 里显式声明真实分类。
    effect_kind: str = "pure"
    """effect 分类：pure / idempotent / reconcilable / external_non_idempotent。"""

    idempotency_key: str | None = None
    """幂等键（None=无）；idempotent/reconcilable 类可据此在恢复时去重。"""

    reconciliation: str = "none"
    """恢复策略：none / query / retry / manual；与 effect_kind 的组合受 ADR 0025 约束。"""

    can_suspend: bool = False
    """该工具是否可能抛 SuspendSignal（HITL/长挂起）。strict audit 只接受 False。"""

    def to_ref(self) -> ToolSpecRef:
        return ToolSpecRef(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )
