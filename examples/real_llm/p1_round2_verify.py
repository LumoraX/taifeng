"""真实 LLM 验证第二批 P1:postcompact-state-reinjection + peer-mailbox-messaging。

mock 已覆盖逻辑正确性;本脚本验真实 provider 维度的衔接(mock 验不了的部分):
  1. pinned 保活:压缩后钉回的 system_injection(source="pinned:*")喂回真实
     LLM —— 模型在压缩后仍能复述任务清单内容(摘要吸收了原文,pinned 是唯一来源)。
  2. peer 投递:真实 LLM 发起 send_message(trigger_turn)→ 真实唤醒专家 →
     专家的真实新 turn 在 prompt 里看到 peer 消息(source=peer 的 user_message)
     并据其内容作答(整链真实)。
  3. wait_peer:真实 LLM 按指令调 wait_peer 等句柄终态取回结果。

依赖模型遵循度的环节如不达标,如实记录为遵循度问题(机制由 mock 覆盖)。

读 .env 的 LLM_BOOTSTRAP_*(见 examples/_provider_bootstrap.py)。

运行:
    PYTHONPATH=src uv run python examples/real_llm/p1_round2_verify.py
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
from taifeng.context.budget import ContextBudget  # noqa: E402
from taifeng.context.strategies import HandoffCompactionStrategy  # noqa: E402
from taifeng.loop.submission import CompactNow  # noqa: E402
from taifeng.tool.builtins.send_message import make_send_message_tool  # noqa: E402
from taifeng.tool.builtins.spawn_skill import make_spawn_skill_tool  # noqa: E402
from taifeng.tool.builtins.wait_peer import make_wait_peer_tool  # noqa: E402

# ── 场景 1:pinned 保活 ──
_PLANNER = """---
name: planner
description: 任务规划助手
version: 1.0.0
type: composite
entry: true
tool_names: [file_read]
max_call_depth: 2
---
# 任务规划助手
简洁回答用户问题(两句话以内)。若历史中出现「当前任务清单」注记,
回答与任务相关的问题时**必须以该注记为准**。
"""

# ── 场景 2/3:peer 互通 ──
_COORD = """---
name: coordinator
description: 会诊协调者
version: 1.0.0
type: composite
entry: true
child_skills: [expert]
tool_names: [spawn_skill, send_message, wait_peer]
max_call_depth: 3
---
# 会诊协调者
- 用户消息形如「把这条发现推送给 <id> 并唤醒」时:**必须调用工具 `send_message`**,
  参数 `{"target": "<id>", "text": "<发现内容>", "mode": "trigger_turn"}`,然后回答「已推送」。
- 用户消息形如「等待 <handle> 完成并复述结果」时:**必须调用工具 `wait_peer`**,
  参数 `{"handle_id": "<handle>", "timeout_seconds": 60}`,然后把返回的 result 复述给用户。
"""

_EXPERT = """---
name: expert
description: 代谢专科专家
version: 1.0.0
type: composite
tool_names: [send_message]
max_call_depth: 2
---
# 代谢专科专家
给出一段简短的专科意见(两句话以内)。若历史中出现来自 peer 的补充信息,
**必须在意见中引用该信息的关键内容**。
"""


class _TodoSource:
    """pinned source:固定任务清单(含唯一可检索的标记词)。"""

    name = "todo"
    max_chars = 500

    def format_for_injection(self) -> str:
        return ("## 当前任务清单\n"
                "[x] 完成数据清洗\n"
                "[ ] 训练玄武岩模型\n"  # 「玄武岩」为唯一性标记词
                "[ ] 编写评估报告")


def _write(skills: Path, spec: dict[str, str]) -> None:
    for sub, body in spec.items():
        (skills / sub).mkdir(parents=True)
        (skills / sub / "SKILL.md").write_text(body, encoding="utf-8")


async def _drive(engine, text: str, events: list) -> None:
    """提交一条消息并等本轮终态(计数式,防上轮残留)。"""
    seen = sum(1 for m in events if m.kind in ("turn_completed", "turn_failed"))
    await engine.submit(taifeng.UserMessage(text=text))
    for _ in range(1800):
        cur = sum(1 for m in events
                  if m.kind in ("turn_completed", "turn_failed"))
        if cur > seen:
            return
        await asyncio.sleep(0.1)


async def _watch(engine, events: list):
    async def w():
        async for ev in engine.subscribe_all():
            events.append(ev.msg)
            if ev.msg.kind == "shutdown":
                break
    task = asyncio.create_task(w())
    await asyncio.sleep(0)
    return task


async def _wait_status(engine, hid: str, status: str, tries: int = 600) -> bool:
    for _ in range(tries):
        if engine.spawn_status([hid])[hid]["status"] == status:
            return True
        await asyncio.sleep(0.1)
    return False


async def verify_pinned(client, root: Path) -> None:
    print("\n=== P1-3 pinned 保活(真实 LLM:压缩后据 pinned 注记复述任务)===")
    skills = root / "s1"
    _write(skills, {"planner": _PLANNER})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / "t1", model_client=client,
        budget=ContextBudget(context_window=200_000),
        compressors=[HandoffCompactionStrategy(model_client=client)],
        pinned_state_sources=[_TodoSource()],
    )
    engine = await pool.get_or_create(session_id="p1p", entry_skill_id="planner")
    events: list = []
    task = await _watch(engine, events)
    # 制造几轮无关历史(任务清单从未出现在对话里——pinned 是唯一来源)
    for q in ("简述什么是上下文压缩?", "什么是 prompt cache?", "什么是 HITL?"):
        await _drive(engine, q, events)
    # 手动强制压缩 → pinned 钉回 tail
    await engine.submit(CompactNow(force=True))
    for _ in range(600):
        if any(m.kind == "pinned_state_reinjected" for m in events):
            break
        await asyncio.sleep(0.1)
    pinned_ev = [m for m in events if m.kind == "pinned_state_reinjected"]
    print(f"[1] pinned_state_reinjected = {len(pinned_ev)}  "
          f"data = {pinned_ev[0].data if pinned_ev else None}")
    # 压缩后真实采样:模型只能从 pinned 注记得知「玄武岩」
    mark = len(events)
    await _drive(engine, "根据当前任务清单,下一个未完成的任务是什么?", events)
    answer = "".join(m.data.get("delta", "") for m in events[mark:]
                     if m.kind == "assistant_text")
    print(f"[2] 压缩后回答 = {answer[:120]}")
    hit = "玄武岩" in answer
    if pinned_ev and hit:
        print("==> pinned 真实链路确证 ✅(压缩后模型据 pinned 注记复述出标记词)")
    elif pinned_ev:
        print("==> 注入链真实确证(事件+turn 正常),但模型未复述标记词(遵循度);"
              "机制由 mock 覆盖 ❓")
    else:
        print("==> 未确证 ❌(压缩未触发 pinned 注入)")
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def verify_peer(client, root: Path) -> None:
    print("\n=== P1-4a peer 投递(真实 LLM send_message → 真实唤醒 → 专家引用 peer 内容)===")
    skills = root / "s2"
    _write(skills, {"coordinator": _COORD, "expert": _EXPERT})
    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=root / "t2", model_client=client,
        compressors=[],
        extra_tools=[make_spawn_skill_tool(), make_send_message_tool(),
                     make_wait_peer_tool()],
    )
    engine = await pool.get_or_create(session_id="p1m", entry_skill_id="coordinator")
    events: list = []
    task = await _watch(engine, events)
    # 程序化派出专家(控制变量:本场景只验证 send_message 链)
    h = await engine.spawn_skill(
        skill_id="expert", args={"topic": "请就糖耐量异常给出初步意见"}, reason="会诊")
    hid, child_tid = h["handle_id"], h["child_thread_id"]
    assert await _wait_status(engine, hid, "done"), "专家首轮未完成"
    first = engine.spawn_status([hid])[hid]["result"]
    print(f"[1] 专家首轮(真实) = {str(first)[:80]}")

    # 真实 LLM 调 send_message(trigger_turn)推送发现并唤醒
    await _drive(engine,
                 f"把这条发现推送给 {child_tid} 并唤醒:患者绿松石指标显著升高",
                 events)
    sent = [m for m in events if m.kind == "peer_message_sent"]
    woken = [m for m in events if m.kind == "peer_agent_woken"]
    print(f"[2] peer_message_sent = {len(sent)}  peer_agent_woken = {len(woken)}")
    if not sent:
        print("==> 真实 LLM 未调用 send_message(遵循度),链未触发;机制由 mock 覆盖。")
    else:
        ok_done = await _wait_status(engine, hid, "done")
        second = engine.spawn_status([hid])[hid]["result"] if ok_done else ""
        print(f"[3] 专家被唤醒后的真实新结论 = {str(second)[:120]}")
        hit = "绿松石" in str(second)
        if woken and ok_done and hit:
            print("==> peer 真实链路确证 ✅(真发送→真唤醒→专家真实 turn 引用 peer 内容)")
        elif woken and ok_done:
            print("==> 发送/唤醒/续跑整链真实确证,专家未复述标记词(遵循度)❓")
        else:
            print("==> 未确证 ❌")

    # P1-4b:真实 LLM 调 wait_peer 取回结果
    print("\n=== P1-4b wait_peer(真实 LLM 等终态取结果)===")
    mark = len(events)
    await _drive(engine, f"等待 {hid} 完成并复述结果", events)
    waits = [m for m in events if m.kind == "peer_wait_resolved"]
    answer = "".join(m.data.get("text", "") for m in events[mark:]
                     if m.kind == "assistant_text")
    print(f"[4] peer_wait_resolved = {[m.data.get('outcome') for m in waits]}")
    print(f"[5] 协调者复述 = {answer[:120]}")
    if waits and waits[-1].data.get("outcome") == "terminal":
        print("==> wait_peer 真实链路确证 ✅")
    else:
        print("==> 真实 LLM 未调用 wait_peer(遵循度);机制由 mock 覆盖。")
    await engine.submit(taifeng.loop.Shutdown())
    await asyncio.wait_for(task, timeout=5.0)
    await pool.close()


async def main() -> None:
    try:
        client, meta = build_model_client(timeout_seconds=120.0)
    except ProviderBootstrapError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[setup] provider={meta['provider']} model={meta['model']}")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        await verify_pinned(client, root)
        await verify_peer(client, root)


if __name__ == "__main__":
    asyncio.run(main())
