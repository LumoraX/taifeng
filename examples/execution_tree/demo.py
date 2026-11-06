"""Sim 执行树：两个同名 Skill 与独立任务并发，含真实工具事实。

运行：PYTHONPATH=src uv run python examples/execution_tree/demo.py
仅使用 conformance 模拟器，无需 API key。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers.sim import RoutingSimClient, SimTurn
from taifeng.telemetry.terminal_tree import TerminalTreeSink


def _client() -> RoutingSimClient:
    """前两个 worker 先并发调用工具，再分别完成。"""
    calls = [{
        'id': f'read_{i}', 'name': 'read_skill',
        'arguments': '{"skill_id":"facts"}',
    } for i in range(2)]
    parallel = [{
        'id': f'child_{i}', 'name': 'call_skill',
        'arguments': json.dumps({'skill_id': skill, 'args': {'task': task},
                                 'reason': '并行演示'}),
    } for i, (skill, task) in enumerate([('worker', '甲'), ('worker', '乙'), ('other', '丙')])]
    return RoutingSimClient(routes={
        'TREE_COORDINATOR_MARK': [SimTurn(tool_calls=parallel), SimTurn(text='全部任务完成。')],
        'TREE_WORKER_MARK': [
            SimTurn(tool_calls=[calls[0]], delay_seconds=0.02),
            SimTurn(tool_calls=[calls[1]], delay_seconds=0.02),
            SimTurn(text='第一份事实整理完成。'),
            SimTurn(text='第二份事实整理完成。'),
        ],
        'TREE_OTHER_MARK': [SimTurn(text='独立任务完成。', delay_seconds=0.02)],
    })


async def main() -> None:
    """订阅完整树并核验同名 Skill 未合并、工具确实执行。"""
    with tempfile.TemporaryDirectory() as temporary:
        pool = await taifeng.EnginePool.create(
            skills_dir=Path(__file__).parent / 'skills',
            threads_dir=Path(temporary) / 'threads', model_client=_client(),
            compressors=[], max_parallel_tool_calls=4,
        )
        engine = await pool.get_or_create(session_id='execution-tree-demo',
                                          entry_skill_id='coordinator')
        sink = TerminalTreeSink()
        task = asyncio.create_task(sink.attach(engine))
        await asyncio.sleep(0)
        submission = await engine.submit(taifeng.UserMessage(text='并行整理两份事实并完成另一任务。'))
        saw_tool = False
        async for ev in engine.subscribe_all():
            if ev.submission_id != submission:
                continue
            saw_tool |= ev.msg.kind == 'tool_call_started' and ev.msg.data.get('name') == 'read_skill'
            if ev.msg.kind == 'turn_failed':
                raise RuntimeError(str(ev.msg.data))
            if ev.msg.kind == 'turn_completed' and ev.msg.data.get('is_root'):
                break
        await pool.close()
        await asyncio.wait_for(task, 5)
        snapshots = sink.projection.snapshot()
        workers = [node for node in snapshots if node.skill_id == 'worker']
        assert len(workers) == 2 and workers[0].execution_id != workers[1].execution_id
        assert all(node.parent_execution_id is not None for node in workers)
        assert saw_tool and all(node.state == 'completed' for node in snapshots)
        assert not sink.projection.incomplete
        print('模拟验证通过：同 Skill 两次并发、独立任务、工具与父子关系均已观察。')


if __name__ == '__main__':
    asyncio.run(main())
