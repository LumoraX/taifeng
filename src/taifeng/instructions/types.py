"""Instructions 数据类型 —— InstructionContext / InstructionLayer / ResolvedInstruction。

设计参见 ``docs/architecture/capabilities/instructions-injection.md``。

字段约束：
    - ``InstructionLayer.cache_ttl_seconds`` SHALL ≥ 0（负值在构造时 raise ValueError）。
    - 所有数据类 frozen → 业务侧拿到 snapshot 后修改 SHALL raise FrozenInstanceError。
    - ``source`` 字段为 ``str | InstructionSource``；``InstructionContext.cancel`` 为
      ``CancellationToken | None``。Phase 0 用 ``Any`` 避免循环 import，但 doc 注释保留协议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

InstructionScope = Literal["engine", "session", "turn"]


@dataclass(frozen=True)
class InstructionContext:
    """传给 ``InstructionSource.fetch`` 的只读上下文。

    业务侧 fetch 实现根据这些字段决定返回哪段指令（如按 ``metadata`` 中业务自定义
    的不透明键路由）。

    Attributes:
        session_id: 来自 ``EnginePool.get_or_create`` 的 session_id。
        thread_id: 来自 ``MessageStore.create_thread`` 的 thread_id。
        entry_skill_id: 当前 turn 的入口 skill id。
        turn_index: 0-based，engine 实例内累计。
        metadata: 业务侧不透明上下文（无业务命名字段，R1）；engine 原样透传，
            taifeng 不解析其 keys。业务侧若需按领域上下文路由，自行从此 dict 取
            自定义键。
        cancel: 父 turn 的 ``CancellationToken``；fetch 须在取消时尽快 abort。
    """

    session_id: str
    thread_id: str
    entry_skill_id: str
    turn_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    cancel: Any = None
    """``CancellationToken | None``；用 ``Any`` 避免循环 import。"""


@dataclass(frozen=True)
class InstructionLayer:
    """单层指令配置。

    - ``source`` 为 ``str`` 时是静态文本（永不重新 fetch；``source_kind='static'``）。
    - ``source`` 为实现 ``InstructionSource`` 协议的对象时是动态拉取
      （``source_kind='dynamic'``，受 ``cache_ttl_seconds`` 控制）。
    - ``cache_ttl_seconds=0`` 表示每次 resolve 都拉（仅对动态 source 生效）。

    Raises:
        ValueError: ``cache_ttl_seconds < 0`` 时构造失败。
    """

    name: str
    """全局唯一，热更键。"""

    source: Any
    """``str | InstructionSource``。"""

    scope: InstructionScope
    cache_ttl_seconds: float = 0
    priority: int = 0
    """升序拼接到 prompt（priority=10 出现在 priority=50 之前）。"""

    cache_volatile: bool = True
    """snapshot 中暴露给业务侧，标记是否破 prompt cache。"""

    def __post_init__(self) -> None:
        # 边界校验：负 ttl 无意义，立即失败
        if self.cache_ttl_seconds < 0:
            raise ValueError(
                f"InstructionLayer(name={self.name!r}): "
                f"cache_ttl_seconds must be >= 0, got {self.cache_ttl_seconds}"
            )


@dataclass(frozen=True)
class ResolvedInstruction:
    """resolve 后的指令快照 —— 通过 ``engine.instructions_snapshot()`` 暴露给业务侧。

    业务侧拿到的是副本（frozen），修改不影响 engine 内部状态。

    Attributes:
        name: 来自 InstructionLayer.name。
        scope: 来自 InstructionLayer.scope。
        text: 实际注入到 prompt 的文本。
        fetched_at: UTC epoch seconds（``time.time()`` 取值）。
        source_kind: ``'static'`` = source 是 str；``'dynamic'`` = 实现协议。
        cache_hit: 当次 resolve 是否命中缓存（动态 source + ttl 内）。
        cache_volatile: 来自 InstructionLayer.cache_volatile（业务侧 cache 调试用）。
    """

    name: str
    scope: str
    text: str
    fetched_at: float
    source_kind: Literal["static", "dynamic"]
    cache_hit: bool = False
    cache_volatile: bool = True
