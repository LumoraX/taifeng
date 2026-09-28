"""Skill 战绩聚合（fitness）协议 —— 认知回路 ⑦ 沉淀相位的第二步（skill-fitness）。

v1（skill-outcome-record）已在每次 ``call_skill`` 子 skill 终态时落一条 ``skill_outcome``
记账项并 emit ``skill_outcome_recorded``；但这些记录散落在各个子 thread 的 JSONL 里，
跨会话聚合（某 skill 近期成功率多少）只能业务自己扫盘。本模块定出聚合存储的协议，
并给一个把事件流接到存储上的 sink 适配器：

- ``SkillFitnessStore``（Protocol）：``record`` 收一条战绩、``fitness`` 读聚合。持久化
  （DB / KV / 指标系统）由业务实现（ADR 0017 规则③：内核只定协议）。
- ``SkillFitnessRecorder``：实现 ``TelemetrySink``，把 ``skill_outcome_recorded`` 事件
  还原为 ``SkillExecutionRecord`` 交给 store；像 ``JsonlSink`` 一样 ``attach(engine)``。
- ``InMemorySkillFitnessStore``：进程内参考实现（测试 / 单进程试用）。

**只沉淀、不决策**：内核任何路径都不读 fitness，不据此改变 skill 的可见性、排序或派发
（召回 / 提拔 / 逐出属于后续相位，另行立项）。长相与战绩分离不变量不变——
``selection_confidence`` 不参与聚合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from taifeng.skill.outcome import SkillExecutionRecord

if TYPE_CHECKING:
    from taifeng.loop.engine import AgentEngine
    from taifeng.loop.event import EventMsg


@dataclass(frozen=True)
class SkillFitness:
    """单个 skill 的战绩聚合（计数口径与 ``OutcomeStatus`` 三态一致）。"""

    skill_id: str
    successes: int = 0
    failures: int = 0
    abandoned: int = 0
    last_ts_unix: int = 0

    @property
    def total(self) -> int:
        """已记录的终态执行次数。"""
        return self.successes + self.failures + self.abandoned


@runtime_checkable
class SkillFitnessStore(Protocol):
    """战绩聚合存储协议（业务实现持久化）。"""

    async def record(self, record: SkillExecutionRecord) -> None:
        """收一条终态战绩。实现须幂等处理同一 ``call_id`` 的重复投递。"""
        ...

    async def fitness(self, skill_id: str) -> SkillFitness | None:
        """读某 skill 的聚合；从未记录过返回 None。"""
        ...


@dataclass
class InMemorySkillFitnessStore:
    """进程内参考实现：按 skill_id 计数，按 call_id 去重。进程退出即丢失。"""

    _fitness: dict[str, SkillFitness] = field(default_factory=dict)
    _seen_calls: set[str] = field(default_factory=set)

    async def record(self, record: SkillExecutionRecord) -> None:
        """累加一条战绩；同一 call_id 只计一次（事件可能被重放投递）。"""
        if record.call_id in self._seen_calls:
            return
        self._seen_calls.add(record.call_id)
        current = self._fitness.get(record.skill_id, SkillFitness(skill_id=record.skill_id))
        self._fitness[record.skill_id] = SkillFitness(
            skill_id=record.skill_id,
            successes=current.successes + (record.outcome == "success"),
            failures=current.failures + (record.outcome == "failure"),
            abandoned=current.abandoned + (record.outcome == "abandoned"),
            last_ts_unix=max(current.last_ts_unix, record.ts_unix),
        )

    async def fitness(self, skill_id: str) -> SkillFitness | None:
        """读聚合；未记录过返回 None。"""
        return self._fitness.get(skill_id)


class SkillFitnessRecorder:
    """把 ``skill_outcome_recorded`` 事件接到 ``SkillFitnessStore`` 的 TelemetrySink。

    其余事件忽略。store 抛出的异常原样上抛——由挂接方（``attach`` 所在的任务）决定处置，
    不在此吞掉。
    """

    def __init__(self, store: SkillFitnessStore) -> None:
        """
        Args:
            store: 业务实现的聚合存储。
        """
        self._store = store

    async def handle(self, ev: EventMsg) -> None:
        """处理一条事件：只转交战绩记录。"""
        if ev.msg.kind != "skill_outcome_recorded":
            return
        await self._store.record(SkillExecutionRecord.from_payload(ev.msg.data))

    async def attach(self, engine: AgentEngine) -> None:
        """订阅 engine 全量事件流并持续转交（与 ``JsonlSink.attach`` 同形）。"""
        async for ev in engine.subscribe_all():
            await self.handle(ev)


__all__ = [
    "InMemorySkillFitnessStore",
    "SkillFitness",
    "SkillFitnessRecorder",
    "SkillFitnessStore",
]
