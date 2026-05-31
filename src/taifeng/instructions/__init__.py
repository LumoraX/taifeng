"""Instructions 注入模块 —— 业务侧把系统指令文本注入到 LLM system prompt。

设计文档：``docs/architecture/capabilities/instructions-injection.md``

Phase 0（当前）：仅协议 + 数据类型已就位；resolver / engine 集成在 change T1-T6 落地。
"""

from taifeng.instructions.resolver import InstructionResolver
from taifeng.instructions.source import InstructionFetchError, InstructionSource
from taifeng.instructions.types import (
    InstructionContext,
    InstructionLayer,
    InstructionScope,
    ResolvedInstruction,
)

__all__ = [
    "InstructionContext",
    "InstructionFetchError",
    "InstructionLayer",
    "InstructionResolver",
    "InstructionScope",
    "InstructionSource",
    "ResolvedInstruction",
]
