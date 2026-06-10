"""子线程挂起 → Resume 续跑链的【边界】测试（test_child_suspend_resume.py 的补强）。

主用例固化了单层子 skill 挂起的幸福路径；本文件补齐 CLAUDE.md「边界必测」要求的
四类边界，均针对 engine 的 _handle_child_resume / _build_resume_chain 续跑链：

  1. 嵌套孙 thread（≥2 层）：祖→父→子（叶）三层派发，叶内挂起 → Resume(叶 thread)
     → 续跑链自根串到叶、核销、逐层回传两级父 call_skill → 根 turn_completed。
  2. 单 turn 多 pending 并发挂起：一个子 thread 内一批 danger×2 同时审批挂起 →
     部分 resume（只给 1 个）被拒（禁部分 resume）→ 全量 resume 两个 tool 都执行回填。
  3. 错误 request_id：Resume 携带 record 里不存在的 request_id → SuspensionResolveRejected
     （incomplete_or_extra_resolutions），record 不被消费，仍可正确续跑。
  4. 重复 resume（幂等边界）：叶挂起被完整核销、根完成后，再次 Resume 同一叶 thread
     → 续跑链找不到活跃挂起 → no_active_suspension 被拒。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from taifeng.suspend.reason import SuspendReason
from taifeng.suspend.record import SuspensionRecord

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# 共享 SKILL.md 片段 —— 每个 skill body 携唯一路由标记，父子互不串扰
# ---------------------------------------------------------------------------

def _composite(
    name: str, mark: str, children: list[str], tools: list[str],
    *, entry: bool = False,
) -> str:
    """生成一个 composite skill 的 SKILL.md 文本（body 含唯一标记 mark）。"""
    entry_line = "entry: true\n" if entry else ""
    return (
        f"---\nname: {name}\ndescription: {name} 测试\nversion: 1.0.0\n"
        f"type: composite\n{entry_line}model: mock-model\n"
        f"child_skills: {children}\ntool_names: {tools}\nmax_call_depth: 4\n---\n"
        f"# {name} {mark}\n测试用 composite skill。\n"
    )


def _atomic(name: str) -> str:
    """生成一个仅用于满足 composite child_skills 校验的占位 atomic skill。"""
    return (
        f"---\nname: {name}\ndescription: {name} 占位\nversion: 1.0.0\n"
        f"type: atomic\n---\n# {name} 占位\n不做实际工作。\n"
    )


def _write_skills(tmp_path: Path, name: str, specs: dict[str, str]) -> Path:
    """把 {skill_id: SKILL.md 文本} 写出到独立 skills 目录，返回目录路径。"""
    root = tmp_path / name
    for sub, body in specs.items():
        d = root / sub
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# 门控 danger 工具 + 事件收集器（与主用例同构，自包含以保持测试独立）
# ---------------------------------------------------------------------------

async def _gated_danger_tool():
    """ask 门控的 danger 工具：透传 ctx.call_id 进 PermissionRequest.metadata。

    SuspendingPrompter 据 metadata.call_id 把 related_call_id 填进 PendingRequest，
    供 resume 定位被挂起的 tool；二次放行返回真实结果用于回填 output。
    parallel_safe=True 让同一 turn 内的多个 danger call 可在一批里并发审批（多 pending）。
    """
    from taifeng.permission.types import PermissionRequest
    from taifeng.tool.spec import ToolContext, ToolResult, ToolSpec

    async def handler(args, ctx: ToolContext) -> ToolResult:
        policy = ctx.extras.get("permission_policy")
        req = PermissionRequest.for_tool_call(
            "danger", args,
            thread_id=ctx.thread_id,
            submission_id=str(ctx.extras.get("submission_id") or ""),
            entry_skill_id=str(ctx.extras.get("entry_skill_id") or ""),
            turn_index=int(ctx.extras.get("turn_index") or 0),
            call_chain=("child",),
            extra_metadata={"call_id": ctx.call_id},
        )
        await policy.check(req)
        return ToolResult.ok(f"danger executed for {ctx.call_id}")

    return ToolSpec(
        name="danger",
        description="需审批的危险工具（边界测试用）",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        parallel_safe=True,
    )


class _AllEventsRecorder:
    """后台 subscribe_all 收集器 —— submit 前启动，避免异步 resume 抢跑丢首批事件。"""

    def __init__(self, engine) -> None:
        self._events: list = []
        self._task = asyncio.create_task(self._run(engine))

    async def _run(self, engine) -> None:
        async for ev in engine.subscribe_all():
            self._events.append(ev)
            if ev.msg.kind == "shutdown":
                break

    async def wait_terminal(self, sub_id: str, *, timeout_s: float = 8.0) -> list:
        """轮询直到 sub_id 的终结事件。

        终结判据：turn_suspended（任意层挂起）/ 根 turn_completed|turn_failed /
        suspension_resolve_rejected（resume 被拒，既不挂起也不完成，本身即终结）。
        """
        async def _poll() -> list:
            while True:
                got = [e for e in self._events if e.submission_id == sub_id]
                for e in got:
                    k = e.msg.kind
                    if k in ("turn_suspended", "suspension_resolve_rejected",
                             "suspension_partially_resolved"):
                        return got
                    if k in ("turn_completed", "turn_failed") and e.msg.data.get("is_root"):
                        return got
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(_poll(), timeout=timeout_s)


def _ask_policy():
    """default ask + 放行 skill_dispatch（call_skill 不挂起，挂起点落在子 thread 内的 tool）。"""
    from taifeng.permission.types import PermissionPolicy, PermissionRule, SuspendingPrompter

    return PermissionPolicy(
        default_mode="ask",
        rules=[PermissionRule(scope="skill_dispatch", target_pattern="glob:*", mode="allow")],
        prompter=SuspendingPrompter(),
    )


async def _suspended_thread_ids(events: list) -> list[str]:
    """从一批事件里取所有 turn_suspended 携带的 thread_id（按出现顺序，去重）。"""
    out: list[str] = []
    for ev in events:
        if ev.msg.kind == "turn_suspended":
            tid = ev.msg.data["thread_id"]
            if tid not in out:
                out.append(tid)
    return out


async def _leaf_with_user_pending(pool, thread_ids: list[str]):
    """在候选 thread 里找出"真正含用户 pending（非 CHILD_SKILL）"的叶 thread。

    续跑链里根/中间层挂起记录的 pending 是 CHILD_SKILL（纯内核派发态），只有叶 thread
    的 pending 才是 PERMISSION/FORM/DATA 等用户待办。Resume 必须指向叶 thread。

    Returns:
        (leaf_thread_id, leaf_SuspensionRecord)；未找到 → (None, None)。
    """
    for tid in thread_ids:
        items = [it async for it in await pool.store.load_thread(tid)]
        susp = [it for it in items if it.kind == "suspension"]
        if not susp:
            continue
        rec = SuspensionRecord.from_item(susp[-1])
        if any(p.reason is not SuspendReason.CHILD_SKILL for p in rec.pending):
            return tid, rec
    return None, None


# ---------------------------------------------------------------------------
# 边界 1：嵌套孙 thread（祖→父→叶 三层）挂起 → Resume 叶 → 逐层回传根完成
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nested_grandchild_suspension_propagates_to_root(tmp_path: Path, threads_dir):
    """三层派发：叶内 danger 挂起 → Resume(叶 thread) → 续跑链跨两级父回传 → 根 completed。"""
    import taifeng
    from taifeng.llm.providers import MockTurn
    from taifeng.llm.providers.mock import RoutingMockClient
    from taifeng.loop.submission import Resume

    skills = _write_skills(tmp_path, "nested_skills", {
        "gp-orch": _composite("gp-orch", "GP_MARK", ["mid-orch"], [], entry=True),
        "mid-orch": _composite("mid-orch", "MID_MARK", ["leaf-worker"], []),
        "leaf-worker": _composite("leaf-worker", "LEAF_MARK", ["noop"], ["danger"]),
        "noop": _atomic("noop"),
    })
    client = RoutingMockClient(routes={
        # 祖：派 mid → （回传后）文本完成
        "GP_MARK": [
            MockTurn(text="派发 mid", tool_calls=[
                {"id": "gp_call", "name": "call_skill",
                 "arguments": '{"skill_id": "mid-orch", "reason": "go"}'}]),
            MockTurn(text="祖完成。"),
        ],
        # 父：派 leaf → （回传后）文本完成
        "MID_MARK": [
            MockTurn(text="派发 leaf", tool_calls=[
                {"id": "mid_call", "name": "call_skill",
                 "arguments": '{"skill_id": "leaf-worker", "reason": "go"}'}]),
            MockTurn(text="中完成。"),
        ],
        # 叶：danger 挂起 → （resume 后）文本完成
        "LEAF_MARK": [
            MockTurn(text="叶调 danger", tool_calls=[
                {"id": "leaf_d", "name": "danger", "arguments": "{}"}]),
            MockTurn(text="叶工作完成 LEAF_DONE_MARK"),
        ],
    })

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[await _gated_danger_tool()],
        permission_policy=_ask_policy())
    engine = await pool.get_or_create(session_id="nested-e2e", entry_skill_id="gp-orch")
    root_tid = engine.thread_id

    recorder = _AllEventsRecorder(engine)
    await asyncio.sleep(0)

    # 第一阶段：三层下钻，叶内 danger 挂起 → 逐层上抛至根挂起
    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await recorder.wait_terminal(sub_id)
    assert events1[-1].msg.kind == "turn_suspended"

    # 三层各 emit turn_suspended：叶(用户 pending) + 中(CHILD_SKILL) + 根(CHILD_SKILL)
    susp_tids = await _suspended_thread_ids(events1)
    assert len(susp_tids) == 3, f"祖父三层应各 emit turn_suspended，实得 {susp_tids}"
    leaf_tid, leaf_rec = await _leaf_with_user_pending(pool, susp_tids)
    assert leaf_tid is not None and leaf_tid != root_tid, "叶 thread 必须含用户 pending 且非根"
    assert leaf_rec.pending[0].related_call_id == "leaf_d"
    req_id = leaf_rec.pending[0].request_id

    # 第二阶段：Resume 叶 thread → 续跑链跨 中→祖 两级回传 → 根完成
    resume_sub = await engine.submit(Resume(
        thread_id=leaf_tid, resolutions={req_id: {"granted": True}}))
    events2 = await recorder.wait_terminal(resume_sub)
    kinds2 = [ev.msg.kind for ev in events2]
    await pool.close()

    # 续跑链每层核销各 emit 一次 suspension_resolved（叶 + 中 + 根 共 3 次）
    resolved_cnt = kinds2.count("suspension_resolved")
    assert resolved_cnt == 3, f"叶+中+祖 三级各核销一次，实得 {resolved_cnt}：{kinds2}"
    # 整个 submission 以根 turn_completed 收尾
    assert any(ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root")
               for ev in events2), f"应以根 turn_completed 收尾，实得 {kinds2}"
    # 叶续跑输出落盘 + 被批准 call 补回 function_call_output
    leaf_items = [it async for it in await pool.store.load_thread(leaf_tid)]
    blob = " ".join(str(it.payload) for it in leaf_items)
    assert "LEAF_DONE_MARK" in blob, "叶续跑输出必须落盘"
    fco_ids = {it.payload.get("call_id") for it in leaf_items
               if it.kind == "function_call_output"}
    assert "leaf_d" in fco_ids, "被批准的挂起 call 必须补回 function_call_output"


# ---------------------------------------------------------------------------
# 边界 2：子 thread 单 turn 多 pending 并发挂起 → 部分 resume 被拒 + 全量 resume 成功
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_child_multi_pending_partial_then_complete(tmp_path: Path, threads_dir):
    """子内 danger×2 同批挂起(request 级核销):先 resolve 1 个 → 部分核销(record
    仍活跃、子不续跑);补齐另 1 个 → 全量达成、两 tool 都执行回填、根完成。"""
    import taifeng
    from taifeng.llm.providers import MockTurn
    from taifeng.llm.providers.mock import RoutingMockClient
    from taifeng.loop.submission import Resume

    skills = _write_skills(tmp_path, "multi_skills", {
        "p2": _composite("p2", "P2_MARK", ["worker2"], [], entry=True),
        "worker2": _composite("worker2", "W2_MARK", ["noop2"], ["danger"]),
        "noop2": _atomic("noop2"),
    })
    client = RoutingMockClient(routes={
        "P2_MARK": [
            MockTurn(text="派发 worker", tool_calls=[
                {"id": "p2_call", "name": "call_skill",
                 "arguments": '{"skill_id": "worker2", "reason": "go"}'}]),
            MockTurn(text="父完成。"),
        ],
        # 同一 turn 抛两个 danger call → 一批两 pending
        "W2_MARK": [
            MockTurn(text="子调两个 danger", tool_calls=[
                {"id": "call_a", "name": "danger", "arguments": "{}"},
                {"id": "call_b", "name": "danger", "arguments": "{}"}]),
            MockTurn(text="子工作完成 W2_DONE_MARK"),
        ],
    })

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[await _gated_danger_tool()],
        permission_policy=_ask_policy())
    engine = await pool.get_or_create(session_id="multi-e2e", entry_skill_id="p2")

    recorder = _AllEventsRecorder(engine)
    await asyncio.sleep(0)

    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await recorder.wait_terminal(sub_id)
    assert events1[-1].msg.kind == "turn_suspended"

    susp_tids = await _suspended_thread_ids(events1)
    leaf_tid, leaf_rec = await _leaf_with_user_pending(pool, susp_tids)
    assert leaf_tid is not None
    # 子 thread 一条 record 含两条 PERMISSION pending（对应 call_a / call_b）
    assert len(leaf_rec.pending) == 2, f"应两 pending 并发挂起，实得 {len(leaf_rec.pending)}"
    req_ids = sorted(leaf_rec.request_ids())
    related = {p.related_call_id for p in leaf_rec.pending}
    assert related == {"call_a", "call_b"}, f"两 pending 应分别关联 call_a/call_b，实得 {related}"

    # （a）子集 resume(multi-pending-partial-resume)：只给一个 req_id → 部分核销:
    # 该 call 的 tool 已执行回填,record 仍活跃、子不续跑
    part_sub = await engine.submit(Resume(
        thread_id=leaf_tid, resolutions={req_ids[0]: {"granted": True}}))
    part_events = await recorder.wait_terminal(part_sub)
    partial = [ev for ev in part_events
               if ev.msg.kind == "suspension_partially_resolved"]
    assert partial, f"子集 resume 应部分核销,实得 {[e.msg.kind for e in part_events]}"
    assert partial[0].msg.data["remaining_request_ids"] == [req_ids[1]], \
        "剩余 pending 应恰为未提交的那个 request_id"

    # （b）补齐剩余 → 全量达成 → 两 tool 都执行 + 子续跑回传 → 根完成
    ok_sub = await engine.submit(Resume(
        thread_id=leaf_tid,
        resolutions={req_ids[1]: {"granted": True}}))
    ok_events = await recorder.wait_terminal(ok_sub)
    kinds = [ev.msg.kind for ev in ok_events]
    await pool.close()

    assert any(ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root")
               for ev in ok_events), f"全量 resume 应以根 completed 收尾，实得 {kinds}"
    leaf_items = [it async for it in await pool.store.load_thread(leaf_tid)]
    fco_ids = {it.payload.get("call_id") for it in leaf_items
               if it.kind == "function_call_output"}
    assert {"call_a", "call_b"} <= fco_ids, "两个被批准 call 都必须补回 function_call_output"


# ---------------------------------------------------------------------------
# 边界 3 + 4：错误 request_id 被拒（record 不消费、可恢复）；重复 resume 幂等被拒
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_child_resume_rejects_bad_request_id_and_double_resume(tmp_path: Path, threads_dir):
    """错误 req_id → 拒绝且 record 不消费 → 正确 resume 仍成功；完成后再 resume → 拒绝。"""
    import taifeng
    from taifeng.llm.providers import MockTurn
    from taifeng.llm.providers.mock import RoutingMockClient
    from taifeng.loop.submission import Resume

    skills = _write_skills(tmp_path, "reject_skills", {
        "p3": _composite("p3", "P3_MARK", ["worker3"], [], entry=True),
        "worker3": _composite("worker3", "W3_MARK", ["noop3"], ["danger"]),
        "noop3": _atomic("noop3"),
    })
    client = RoutingMockClient(routes={
        "P3_MARK": [
            MockTurn(text="派发", tool_calls=[
                {"id": "p3_call", "name": "call_skill",
                 "arguments": '{"skill_id": "worker3", "reason": "go"}'}]),
            MockTurn(text="父完成。"),
        ],
        "W3_MARK": [
            MockTurn(text="子调 danger", tool_calls=[
                {"id": "w3_d", "name": "danger", "arguments": "{}"}]),
            MockTurn(text="子完成 W3_DONE_MARK"),
        ],
    })

    pool = await taifeng.EnginePool.create(
        skills_dir=skills, threads_dir=threads_dir, model_client=client,
        compressors=[], extra_tools=[await _gated_danger_tool()],
        permission_policy=_ask_policy())
    engine = await pool.get_or_create(session_id="reject-e2e", entry_skill_id="p3")

    recorder = _AllEventsRecorder(engine)
    await asyncio.sleep(0)

    sub_id = await engine.submit(taifeng.UserMessage(text="go"))
    events1 = await recorder.wait_terminal(sub_id)
    susp_tids = await _suspended_thread_ids(events1)
    leaf_tid, leaf_rec = await _leaf_with_user_pending(pool, susp_tids)
    assert leaf_tid is not None
    req_id = leaf_rec.pending[0].request_id

    # （边界 3）错误 request_id → SuspensionResolveRejected，record 不被消费
    bad_sub = await engine.submit(Resume(
        thread_id=leaf_tid, resolutions={"bogus-request-id": {"granted": True}}))
    bad_events = await recorder.wait_terminal(bad_sub)
    reject = [ev for ev in bad_events if ev.msg.kind == "suspension_resolve_rejected"]
    assert reject, f"错误 req_id 必须被拒，实得 {[e.msg.kind for e in bad_events]}"
    assert "unknown_request_ids" in reject[0].msg.data["reason"]
    # record 未被消费：仍有一条活跃 suspension（无 suspend_resolved marker 核销）
    items_after_bad = [it async for it in await pool.store.load_thread(leaf_tid)]
    assert not any(it.kind == "system_injection"
                   and it.payload.get("source") == "suspend_resolved"
                   for it in items_after_bad), "被拒不得消费 record（无 resolved marker）"

    # 正确 req_id → 续跑回传根完成（证明上一步拒绝未损坏断点）
    ok_sub = await engine.submit(Resume(
        thread_id=leaf_tid, resolutions={req_id: {"granted": True}}))
    ok_events = await recorder.wait_terminal(ok_sub)
    assert any(ev.msg.kind == "turn_completed" and ev.msg.data.get("is_root")
               for ev in ok_events), "正确 resume 应以根 completed 收尾"

    # （边界 4）重复 resume 同一已核销叶 thread → 续跑链找不到活跃挂起 → 拒绝
    dup_sub = await engine.submit(Resume(
        thread_id=leaf_tid, resolutions={req_id: {"granted": True}}))
    dup_events = await recorder.wait_terminal(dup_sub)
    await pool.close()
    dup_reject = [ev for ev in dup_events if ev.msg.kind == "suspension_resolve_rejected"]
    assert dup_reject, f"重复 resume 必须被拒，实得 {[e.msg.kind for e in dup_events]}"
    assert dup_reject[0].msg.data["reason"] == "no_active_suspension"
