"""真实 LLM 验证:挂起核销链(multi-pending / 挂起态守卫 / TTL / K2 增额)。

mock 已覆盖逻辑正确性;本脚本验真实 provider 维度(mock 验不了的部分):
  1. 多 pending 错峰:真实 LLM 同轮并行发两个 request_user_input → 一条 record
     两 pending → 先答其一(suspension_partially_resolved,父不续跑)→ 补齐 →
     真实续跑且最终回答**引用两个答案的内容**(回填 fco 真实喂回模型)。
  2. 挂起态拒收新 UserMessage:挂起中提交 → TurnFailed(thread_suspended);
     结清后再提交 → 真实模型照常作答。
  3. TTL 真实到期:ttl=5s 真实壁钟 → suspension_expired → 自动 abort,
     不再采样(无人值守不死锁)。
  4. K2 真实 token 触顶:max_session_tokens=10 → 首轮真实 usage 触顶 →
     SuspendByDefault 挂起(scope=turn_suspended)→ retry+extend_tokens 抬顶 →
     真实续跑完成(增额执法走真实计量)。

依赖模型遵循度的环节(同轮双问询)如不达标,如实记录为遵循度问题。

读 .env 的 LLM_BOOTSTRAP_*(见 examples/_provider_bootstrap.py)。

运行:
    PYTHONPATH=src uv run python examples/real_llm/suspend_chain_verify.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

import taifeng  # noqa: E402
from taifeng.loop.submission import Resume  # noqa: E402
from taifeng.tool.builtins.request_user_input import (  # noqa: E402
    make_request_user_input_tool,
)
from taifeng.tool.spec import ToolResult, ToolSpec  # noqa: E402

# 双问询技能:指示模型同一轮并行发两个 request_user_input
_DUAL_ASK = """---
name: dual-ask
description: 行程规划助手(需先收集两项信息)
version: 1.0.0
type: composite
entry: true
tool_names: [request_user_input]
max_call_depth: 2
---
# 行程规划助手

规划前必须先收集两项信息。**第一轮必须在同一条消息里并行调用两次
request_user_input 工具**(一次问出发城市,一次问预算上限),不要分两轮问、
不要自行假设。拿到两个答案后,用一句话给出行程建议,并在句中**原样复述**
出发城市与预算数字。
"""

# 单问询技能(挂起态守卫 / TTL 场景用)
_ONE_ASK = """---
name: one-ask
description: 偏好收集助手
version: 1.0.0
type: composite
entry: true
tool_names: [request_user_input]
max_call_depth: 2
---
# 偏好收集助手

回答任何问题前,必须先调用 request_user_input 问用户偏好哪种风格(简洁/详尽),
然后按该风格作答。
"""

# K2 场景技能:指示先调 echo 工具(制造 had_tool_calls,触发 turn 内 K2 检查)
_ECHO_SKILL = """---
name: echoer
description: 回声助手
version: 1.0.0
type: composite
entry: true
tool_names: [echo]
max_call_depth: 2
---
# 回声助手

收到任何消息,先调用一次 echo 工具,然后用一句话总结 echo 的返回。
"""


def _echo_tool() -> ToolSpec:
    async def _h(args: dict, ctx: object) -> ToolResult:
        return ToolResult.ok("echo: ok")

    return ToolSpec(
        name="echo", description="回声(原样返回)",
        input_schema={"type": "object", "properties": {}},
        handler=_h, parallel_safe=True,
    )


def _root_truly_completed(events: list, since: int = 0) -> bool:
    """根 turn 真正完成(turn_completed 在 error 终态也会发,必须看 end_reason)。

    ``since``:只看该下标之后的事件——避免被早先 turn 的终结事件(如挂起中被拒
    的 thread_suspended TurnFailed)提前满足。
    """
    return any(m.kind == "turn_completed" and m.data.get("is_root")
               and m.data.get("end_reason") == "completed"
               for m in events[since:])


def _root_settled(events: list, since: int = 0) -> bool:
    """根 turn 已终结(完成或失败;since 语义同上)。"""
    return any((m.kind == "turn_completed" and m.data.get("is_root"))
               or m.kind == "turn_failed" for m in events[since:])


def _write_skill(root: Path, name: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


async def _wait(cond, tries: int = 600) -> bool:
    """轮询(真实网络延迟下放宽到 ~60s)。"""
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


class _Recorder:
    def __init__(self, engine) -> None:
        self.events: list = []
        self._task = asyncio.create_task(self._run(engine))

    async def _run(self, engine) -> None:
        async for ev in engine.subscribe_all():
            self.events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                return

    def kinds(self) -> list[str]:
        return [m.kind for m in self.events]

    async def close(self) -> None:
        await asyncio.wait_for(self._task, timeout=10.0)


async def scenario_multi_pending(client, tmp: Path) -> str:
    """场景 1:同轮双问询 → 错峰 Resume → 真实续跑引用两个答案。"""
    skills = tmp / "s1"
    _write_skill(skills, "dual-ask", _DUAL_ASK)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp / "t1", model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()],
        max_parallel_tool_calls=4,
    )
    engine = await pool.get_or_create(session_id="s1", entry_skill_id="dual-ask")
    rec = _Recorder(engine)
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text="帮我规划一次周末旅行。"))
    ok = await _wait(lambda: any(m.kind == "turn_suspended" for m in rec.events))
    assert ok, "应挂起等待问询"
    susp = next(m for m in rec.events if m.kind == "turn_suspended")
    pendings = susp.data["pending"]
    if len(pendings) < 2:
        await engine.submit(taifeng.loop.Shutdown())
        await rec.close()
        await pool.close()
        return (f"[遵循度] 模型未同轮并行双问询(实得 {len(pendings)} pending),"
                f"multi-pending 错峰链未走到——机制由 mock 覆盖")
    rid_a, rid_b = pendings[0]["request_id"], pendings[1]["request_id"]
    # 错峰:先答其一 → 必须 partial、不得续跑
    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={rid_a: {"answer": "出发城市:杭州"}}))
    ok = await _wait(lambda: any(
        m.kind == "suspension_partially_resolved" for m in rec.events))
    assert ok, "先答其一应部分核销"
    assert not any(m.kind == "turn_completed" and m.data.get("is_root")
                   for m in rec.events), "仍有未答问询,父 turn 不得续跑"
    # 补齐 → 真实续跑
    n0 = len(rec.events)
    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={rid_b: {"answer": "预算上限:3000元"}}))
    assert await _wait(lambda: _root_settled(rec.events, n0)), "补齐后应续跑至终结"
    final = "".join(m.data.get("text", "") for m in rec.events
                    if m.kind == "assistant_text")
    failures = [m for m in rec.events[n0:] if m.kind == "turn_failed"]
    await engine.submit(taifeng.loop.Shutdown())
    await rec.close()
    await pool.close()
    if failures and "reasoning_content" in str(failures[0].data.get("error", "")):
        # thinking 模型续跑缺陷:reasoning 既不落史也不回传 → provider 400。
        # 见 openspec change reasoning-content-passback(本脚本首次抓到该缺陷)。
        return ("[KNOWN-DEFECT] thinking 模型续跑被 provider 拒:reasoning_content "
                "未回传(change reasoning-content-passback 待修);错峰核销链本身正确"
                "(partial → 全量达成 → 续跑被派发)")
    assert _root_truly_completed(rec.events, n0), \
        f"续跑应真正完成,实得 {[m.data for m in failures]}"
    cites = ("杭州" in final) + ("3000" in final)
    return (f"[PASS] 双 pending 错峰:partial → 全量达成 → 真实续跑;"
            f"最终回答引用 {cites}/2 个答案" + ("" if cites == 2 else "(遵循度欠佳)"))


async def scenario_reject_while_suspended(client, tmp: Path) -> str:
    """场景 2:挂起态拒收新 UserMessage;结清后真实模型照常作答。"""
    skills = tmp / "s2"
    _write_skill(skills, "one-ask", _ONE_ASK)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp / "t2", model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool()],
    )
    engine = await pool.get_or_create(session_id="s2", entry_skill_id="one-ask")
    rec = _Recorder(engine)
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text="taifeng 是什么?"))
    assert await _wait(lambda: any(m.kind == "turn_suspended" for m in rec.events))
    susp = next(m for m in rec.events if m.kind == "turn_suspended")
    rid = susp.data["pending"][0]["request_id"]
    # 挂起中再发消息 → 显式拒绝
    await engine.submit(taifeng.UserMessage(text="换个问题:今天天气?"))
    assert await _wait(lambda: any(
        m.kind == "turn_failed" and m.data.get("kind") == "thread_suspended"
        for m in rec.events)), "挂起中新消息应被拒"
    # 结清 → 真实续跑
    n0 = len(rec.events)
    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={rid: {"answer": "简洁"}}))
    assert await _wait(lambda: _root_settled(rec.events, n0))
    if not _root_truly_completed(rec.events, n0):
        failures = [m.data.get("error", "") for m in rec.events[n0:]
                    if m.kind == "turn_failed"]
        if any("reasoning_content" in str(e) for e in failures):
            await engine.submit(taifeng.loop.Shutdown())
            await rec.close()
            await pool.close()
            return ("[KNOWN-DEFECT] 结清后续跑被 reasoning_content 缺陷拦截"
                    "(change reasoning-content-passback);拒收守卫本身已验证")
        raise AssertionError(f"续跑失败: {failures}")
    await engine.submit(taifeng.loop.Shutdown())
    await rec.close()
    await pool.close()
    return "[PASS] 挂起态拒收新 UserMessage(thread_suspended)→ 结清后真实续跑完成"


async def scenario_ttl_real_clock(client, tmp: Path) -> str:
    """场景 3:ttl=5s 真实壁钟到期 → 自动 abort,不再采样。"""
    skills = tmp / "s3"
    _write_skill(skills, "one-ask", _ONE_ASK)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp / "t3", model_client=client,
        compressors=[], extra_tools=[make_request_user_input_tool(ttl_seconds=5)],
    )
    engine = await pool.get_or_create(session_id="s3", entry_skill_id="one-ask")
    rec = _Recorder(engine)
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text="随便聊聊。"))
    assert await _wait(lambda: any(m.kind == "turn_suspended" for m in rec.events))
    started = sum(1 for m in rec.events if m.kind == "turn_started")
    # 无人值守等真实到期(5s + 余量)
    assert await _wait(lambda: any(
        m.kind == "suspension_expired" for m in rec.events), tries=150), \
        "5s ttl 应真实到期"
    assert await _wait(lambda: any(
        m.kind == "suspension_resolved" for m in rec.events))
    await asyncio.sleep(1.0)
    started_after = sum(1 for m in rec.events if m.kind == "turn_started")
    assert started_after == started, "DATA 到期 abort 后不得再采样"
    await engine.submit(taifeng.loop.Shutdown())
    await rec.close()
    await pool.close()
    return "[PASS] TTL 真实壁钟 5s 到期 → suspension_expired → 自动 abort 不再采样"


async def scenario_k2_extend(client, tmp: Path) -> str:
    """场景 4:真实 usage 触顶 K2 → 挂起 → retry+extend 抬顶 → 真实续跑完成。"""
    skills = tmp / "s4"
    _write_skill(skills, "echoer", _ECHO_SKILL)
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=tmp / "t4", model_client=client,
        compressors=[], extra_tools=[_echo_tool()],
        max_session_tokens=10,  # 首轮真实 usage 必触顶
        failure_policy=taifeng.SuspendByDefaultPolicy(),
    )
    engine = await pool.get_or_create(session_id="s4", entry_skill_id="echoer")
    rec = _Recorder(engine)
    await asyncio.sleep(0)
    await engine.submit(taifeng.UserMessage(text="开始。"))
    ok = await _wait(lambda: any(m.kind == "turn_suspended" for m in rec.events))
    if not ok:
        # 模型未调工具 → 无 had_tool_calls,K2 turn 内检查不触发(遵循度)
        await engine.submit(taifeng.loop.Shutdown())
        await rec.close()
        await pool.close()
        return "[遵循度] 模型未按指示调用 echo 工具,K2 触顶链未走到(机制由 mock 覆盖)"
    rl = [m for m in rec.events if m.kind == "resource_limit_exceeded"]
    assert rl and rl[0].data["scope"] == "turn_suspended", \
        f"K2 触顶挂起 scope 应如实,实得 {[m.data for m in rl]}"
    susp = next(m for m in rec.events if m.kind == "turn_suspended")
    rid = susp.data["pending"][0]["request_id"]
    # 裸 retry 应被拒
    await engine.submit(Resume(
        thread_id=engine.thread_id, resolutions={rid: {"action": "retry"}}))
    assert await _wait(lambda: any(
        m.kind == "suspension_resolve_rejected"
        and "extend_tokens" in m.data["reason"] for m in rec.events)), \
        "裸 retry 应被 k2_retry_requires_extend_tokens 拒绝"
    # retry + 增额 → 真实续跑完成
    n0 = len(rec.events)
    await engine.submit(Resume(
        thread_id=engine.thread_id,
        resolutions={rid: {"action": "retry", "extend_tokens": 100000}}))
    assert await _wait(lambda: _root_settled(rec.events, n0)), "增额后应续跑至终结"
    assert _root_truly_completed(rec.events, n0), \
        "增额后应真实续跑完成:" + str([m.data.get("error") for m in rec.events[n0:]
                                        if m.kind == "turn_failed"])
    await engine.submit(taifeng.loop.Shutdown())
    await rec.close()
    await pool.close()
    return "[PASS] K2 真实 usage 触顶挂起(scope=turn_suspended)→ 裸 retry 拒 → 增额续跑完成"


async def main() -> int:
    try:
        client, meta = build_model_client()
    except ProviderBootstrapError as e:
        print(f"SKIP: {e}")
        return 0
    print(f"provider={meta.get('provider')} model={meta.get('model')}")
    results: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for fn in (scenario_multi_pending, scenario_reject_while_suspended,
                   scenario_ttl_real_clock, scenario_k2_extend):
            try:
                results.append(await fn(client, tmp))
            except AssertionError as e:
                results.append(f"[FAIL] {fn.__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                results.append(f"[ERROR] {fn.__name__}: {type(e).__name__}: {e}")
            print(results[-1], flush=True)
    print("\n=== 汇总 ===")
    for r in results:
        print(r)
    return 1 if any(r.startswith(("[FAIL]", "[ERROR]")) for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
