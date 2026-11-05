"""执行事件的不可变身份；仅作观测，不进入模型上下文或持久真相。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExecutionIdentity(BaseModel):
    """thread 标识逻辑节点，attempt 标识一次运行或续跑。"""

    model_config = ConfigDict(frozen=True)

    execution_id: str
    attempt_id: str
    thread_id: str
    skill_id: str
    model: str = 'auto'
    parent_execution_id: str | None = None
    parent_thread_id: str | None = None
