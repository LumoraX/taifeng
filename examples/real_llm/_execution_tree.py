"""执行树真实取证：模型自主双路派发，同 skill 的子执行保持独立身份与用量。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

from _drivers import _wait_for
from _setups import ScenarioSetup

from taifeng import ToolResult, ToolSpec, UserMessage
from taifeng.llm.providers import SimClient, SimTurn

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng.llm.types import ApiRequest

ROOT_SKILL = "observation-root"
WORKER_SKILL = "observation-worker"
PROBE_TOOL = "observation_probe"
ROOT_MARK = "EXECUTION_ROOT_MARK"
WORKER_MARK = "EXECUTION_WORKER_MARK"


def setup_execution_tree(root: Path, client: Any) -> ScenarioSetup:
    """装配有界探针；仅两个实际 handler 都进入后，driver 才允许它们退出。"""
    release = asyncio.Event()
    state: dict[str, Any] = {"release": release, "probes": []}

    async def probe(args: dict[str, Any], ctx: Any) -> ToolResult:
        """记录实际 thread 与进入/离开时间，不把观测身份返回给模型。"""
        ctx.cancel.raise_if_cancelled()
        record = {"thread_id": ctx.thread_id, "call_id": ctx.call_id,
                  "label": args.get("label"), "entered": time.monotonic(), "exited": None}
        state["probes"].append(record)
        try:
            await asyncio.wait_for(release.wait(), timeout=150.0)
            ctx.cancel.raise_if_cancelled()
            return ToolResult.ok(f"OBSERVED[{args['label']}]")
        finally:
            record["exited"] = time.monotonic()

    tool = ToolSpec(
        name=PROBE_TOOL, description="观测一次指定 label，返回 OBSERVED[label]。",
        input_schema={"type": "object", "properties": {"label": {
            "type": "string", "enum": ["left", "right"]}}, "required": ["label"],
            "additionalProperties": False}, handler=probe, parallel_safe=True,
    )
    return ScenarioSetup(extra_tools=[tool], state=state, pool_kwargs={
        "enable_request_capture": True, "max_parallel_tool_calls": 2,
    })


def _nodes(res: Any) -> dict[str, Any]:
    """逐条验证身份稳定、父子谱系与同 submission，返回三个执行节点。"""
    root = res.state["root_thread_id"]
    nodes: dict[str, Any] = {}
    required = {"turn_started", "execution_updated", "assistant_text", "tool_call_started",
                "tool_call_completed", "tool_batch_dispatched", "turn_completed",
                "llm_request_recorded", "skill_dispatched"}
    for envelope in res.envelopes:
        if envelope.msg.kind not in required:
            continue
        identity = envelope.execution
        assert identity is not None, "执行事实缺少 identity"
        assert envelope.submission_id == res.state["submission_id"], "同步子执行串了 submission"
        assert identity.execution_id == identity.thread_id, "节点与 thread 不一致"
        previous = nodes.setdefault(identity.execution_id, identity)
        assert previous == identity, "同一节点执行身份发生变化"
        if identity.execution_id == root:
            assert identity.skill_id == ROOT_SKILL and identity.parent_execution_id is None
            assert identity.parent_thread_id is None, "根节点错误携带父 thread"
        else:
            assert identity.skill_id == WORKER_SKILL, "出现未预期的第三类节点"
            assert identity.parent_execution_id == root and identity.parent_thread_id == root, "子节点父级错误"
    assert len(nodes) == 3 and root in nodes, "必须有根与两个不同的同 skill 子执行"
    assert len({node.attempt_id for node in nodes.values()}) == 3, "attempt 未按执行隔离"
    return nodes


def _verify_probes(res: Any, nodes: dict[str, Any]) -> None:
    """检查真实 handler 重叠，并把调用开始、结束和正文逐一归回各自子 thread。"""
    probes = res.state["probes"]
    root = res.state["root_thread_id"]
    assert len(probes) == 2 and {p["label"] for p in probes} == {"left", "right"}, "探针调用数错误"
    assert {p["thread_id"] for p in probes} == nodes.keys() - {root}, "探针没有落在两个子节点"
    assert all(p["exited"] is not None for p in probes), "探针未退出"
    assert max(p["entered"] for p in probes) < min(p["exited"] for p in probes), "探针未实际交叠"
    for probe in probes:
        owned = [ev.msg for ev in res.envelopes if ev.execution is not None
                 and ev.execution.thread_id == probe["thread_id"]]
        started = [msg for msg in owned if msg.kind == "tool_call_started"
                   and msg.data.get("call_id") == probe["call_id"]]
        done = [msg for msg in owned if msg.kind == "tool_call_completed"
                and msg.data.get("call_id") == probe["call_id"]]
        assert len(started) == len(done) == 1, "探针事件归属错误或缺失"
        assert started[0].data["name"] == PROBE_TOOL and not done[0].data["is_error"]
        assert json.loads(started[0].data["arguments"])["label"] == probe["label"]
        marker = f"OBSERVED[{probe['label']}]"
        assert marker in done[0].data["output"], "探针输出不匹配 label"
        text = "".join(msg.data["delta"] for msg in owned if msg.kind == "assistant_text")
        assert marker in text, "子执行正文没有归属对应 thread"
        other = "right" if probe["label"] == "left" else "left"
        assert f"OBSERVED[{other}]" not in text, "兄弟执行正文发生串流"


def _token_count(usage: dict[str, Any]) -> int:
    """兼容 provider 缺省 total_tokens，仍核对明确的输入输出计数。"""
    assert all(type(usage.get(key)) is int and usage[key] >= 0
               for key in ("input_tokens", "output_tokens")), "usage 输入输出计数无效"
    total = usage.get("total_tokens", 0) or usage["input_tokens"] + usage["output_tokens"]
    assert type(total) is int and total == usage["input_tokens"] + usage["output_tokens"], "usage 总量错误"
    return total


def _verify_usage(res: Any, nodes: dict[str, Any]) -> dict[str, int]:
    """每个 attempt 最后自身快照与自身终态、thread 台账一致，总和只计一次。"""
    totals: dict[str, int] = {}
    for thread in nodes:
        owned = [ev.msg for ev in res.envelopes if ev.execution is not None
                 and ev.execution.thread_id == thread]
        updates = [msg.data for msg in owned if msg.kind == "execution_updated"]
        assert len(updates) >= 4, "缺少采样前后执行快照"
        assert {update["context_measured"] for update in updates} == {False, True}, "缺少估算或实测上下文"
        previous = 0
        previous_input = 0
        for update in updates:
            assert type(update["context_measured"]) is bool, "上下文口径必须明确"
            assert type(update["context_tokens"]) is int and update["context_tokens"] >= 0
            assert type(update["context_window"]) is int and update["context_window"] > 0
            assert type(update["iteration"]) is int and update["iteration"] >= 1
            assert update["phase"] == "model", "采样未正常结束"
            count = _token_count(update["usage"])
            assert count >= previous, "自身累计用量发生回退"
            if update["context_measured"]:
                assert update["context_tokens"] == update["usage"]["input_tokens"] - previous_input, "实测上下文与本次采样输入不一致"
                previous_input = update["usage"]["input_tokens"]
            previous = count
        assert updates[-1]["usage_complete"] is True, "自身用量不完整"
        completed = [msg.data for msg in owned if msg.kind == "turn_completed"]
        assert len(completed) == 1 and completed[0]["success"], "执行没有成功终态"
        assert completed[0]["usage_complete"] is True, "终态用量缺失"
        assert _token_count(completed[0]["usage"]) == previous > 0, "快照与终态自身用量不一致"
        assert res.state["introspection"]["usage"]["by_thread"][thread]["total_tokens"] == previous
        totals[thread] = previous
    assert sum(totals.values()) == res.state["introspection"]["usage"]["total_tokens"], "整树用量重复或漏计"
    return totals


def _verify_requests(res: Any, nodes: dict[str, Any]) -> None:
    """观测身份字段及其值不得出现在任何实际模型请求中。"""
    requests = [ev.msg.data for ev in res.envelopes if ev.msg.kind == "llm_request_recorded"]
    assert len(requests) >= 6, "没有捕获父子实际请求"
    hidden = {"execution_id", "attempt_id", "parent_execution_id", "parent_thread_id",
              *(node.execution_id for node in nodes.values()),
              *(node.attempt_id for node in nodes.values())}
    for request in requests:
        blob = json.dumps(request, ensure_ascii=False)
        assert all(value not in blob for value in hidden), "执行元数据泄露到模型请求"


def verify_execution_evidence(res: Any) -> dict[str, Any]:
    """纯函数核对完整 envelope 与探针事实，不能靠事件出现或模型答案碰巧正确通过。"""
    assert not any(ev.msg.kind == "turn_failed" for ev in res.envelopes), "执行树出现失败"
    nodes = _nodes(res)
    root = res.state["root_thread_id"]
    own = [ev.msg for ev in res.envelopes if ev.execution is not None
           and ev.execution.execution_id == root]
    assert any(msg.kind == "tool_batch_dispatched" and msg.data["count"] == 2
               and msg.data["max_parallel"] >= 2 for msg in own), "根未自主双路并行派发"
    dispatched = [msg for msg in own if msg.kind == "skill_dispatched"]
    assert len(dispatched) == 2 and all(msg.data["skill_id"] == WORKER_SKILL
                                      for msg in dispatched), "不是同 skill 两次派发"
    _verify_probes(res, nodes)
    totals = _verify_usage(res, nodes)
    _verify_requests(res, nodes)
    completed = [msg.data for msg in own if msg.kind == "turn_completed"]
    assert len(completed) == 1 and completed[0]["is_root"] is True, "根未真正完成"
    total = sum(totals.values())
    assert _token_count(completed[0]["subtree_usage"]) == total > totals[root], "根自身用量与子树混淆"
    root_text = "".join(msg.data["delta"] for msg in own if msg.kind == "assistant_text")
    assert all(f"OBSERVED[{label}]" in root_text for label in ("left", "right")), "根未汇总两个结果"
    return {"root": root, "children": sorted(nodes.keys() - {root}),
            "own_tokens": totals[root], "session_tokens": total}


async def drive_execution_tree(engine: Any, res: Any) -> None:
    """先确认两个探针同时在飞，再释放；以根完整 submission 收尾作为取证边界。"""
    res.state["root_thread_id"] = engine.thread_id
    submission = await engine.submit(UserMessage(text=(
        "请在第一条响应中同时调用两次 call_skill，skill_id 都为 observation-worker，"
        "args 分别是 {\"label\":\"left\"} 与 {\"label\":\"right\"}。"
        "必须在同一次响应里派发两次，不能等待第一个结束后再发第二个。"
        "收到后原样汇总两项 OBSERVED[...]。")))
    res.state["submission_id"] = submission
    try:
        await _wait_for(res, lambda msg: len(res.state["probes"]) >= 2
                        or msg.kind == "turn_failed"
                        or (msg.kind == "turn_completed" and msg.data.get("is_root")),
                        wait_seconds=120.0, what="两个实际探针同时进入")
        assert len(res.state["probes"]) == 2, "模型未自主派发两条探针"
        assert all(p["exited"] is None for p in res.state["probes"]), "释放前探针已退出"
    finally:
        res.state["release"].set()
    async def consume() -> None:
        """消费到根 submission 的真实自然终态，避免只见子节点完成就提前通过。"""
        async for _ in engine.subscribe(submission):
            pass
    await asyncio.wait_for(consume(), timeout=240.0)
    await _wait_for(res, lambda msg: msg.kind == "turn_completed" and msg.data.get("is_root"),
                    wait_seconds=5.0, what="采集根完整终态")
    res.state["introspection"] = engine.introspect()
    res.state["evidence"] = verify_execution_evidence(res)
    print(f"  [execution] 同 skill 双子节点实际重叠；自身用量相加={res.state['evidence']['session_tokens']}")


class ExecutionTreeSimClient(SimClient):
    """按可见请求动态决定双路调用与各自标签，不访问探针或内核隐藏状态。"""

    def __init__(self) -> None:
        """保留完整 conformance 校验，只替换逐次采样脚本选择。"""
        super().__init__(turns=[])

    def _next_turn(self, request: ApiRequest) -> SimTurn:
        """从 skill 正文、用户 seed 和本线程工具结果选择下一动作。"""
        system = "\n".join(request.system_prompt)
        outputs = [str(message.content) for message in request.messages if message.role == "tool"]
        if WORKER_MARK in system:
            seed = "\n".join(str(message.content) for message in request.messages
                             if message.role == "user")
            match = re.search(r'"label"\s*:\s*"(left|right)"', seed)
            assert match is not None, "子请求没有可见 label seed"
            label = match.group(1)
            if outputs:
                return SimTurn(text=outputs[-1])
            return SimTurn(tool_calls=[{"id": f"probe-{label}", "name": PROBE_TOOL,
                                        "arguments": json.dumps({"label": label})}])
        assert ROOT_MARK in system, "请求未归属执行树的已知 skill"
        if outputs:
            markers = re.findall(r"OBSERVED\[(?:left|right)\]", "\n".join(outputs))
            assert set(markers) == {"OBSERVED[left]", "OBSERVED[right]"}, "根模型未见到两个结果"
            return SimTurn(text="；".join(markers))
        return SimTurn(tool_calls=[{
            "id": f"dispatch-{label}", "name": "call_skill", "arguments": json.dumps({
                "skill_id": WORKER_SKILL, "args": {"label": label}, "reason": "独立取证"}),
        } for label in ("left", "right")])


DRIVER = {"execution_observability": drive_execution_tree}
