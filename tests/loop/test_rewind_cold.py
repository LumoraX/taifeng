"""derive_rewind_log 推导 + 冷场景 rewind。"""
from __future__ import annotations

from taifeng.conversation.models import (
    assistant_message,
    function_call,
    function_call_output,
    user_message,
)
from taifeng.loop.rewind import derive_rewind_log

T = "thr"


def _u(): return user_message("u", thread_id=T)
def _a(): return assistant_message("a", thread_id=T, model="m")
def _fc(cid, name="read_skill", args="{}"): return function_call(cid, name, args, thread_id=T)
def _fco(cid): return function_call_output(cid, output="ok", thread_id=T, is_error=False)


def test_derive_single_turn_iterations_and_dispatch():
    """一 turn:[u, a, fc, fco, a](2 圈,首圈 1 派发)→ 2 iteration + 1 dispatch。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1"), _a()]
    nodes = derive_rewind_log(hist)
    ids = [n.node_id for n in nodes]
    assert ids == ["t1:it1", "t1:disp0", "t1:it2"]
    disp = next(n for n in nodes if n.node_id == "t1:disp0")
    assert disp.history_len == 1          # 所属 it1 的 history_len(assistant_message 下标)
    assert disp.inner_history_len == 3    # fc 下标(2)+1
    assert disp.call_id == "c1"


def test_derive_same_iteration_multiple_dispatch_share_history_len():
    """同圈 2 派发:[u, a, fc1, fco1, fc2, fco2] → 两 dispatch 共享 it1 的 history_len=1。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1"), _fc("c2"), _fco("c2")]
    nodes = derive_rewind_log(hist)
    disps = [n for n in nodes if n.kind == "dispatch"]
    assert [d.node_id for d in disps] == ["t1:disp0", "t1:disp1"]
    assert all(d.history_len == 1 for d in disps)        # 归一到 it1 采样前
    assert [d.inner_history_len for d in disps] == [3, 5]  # 各自 fc 下标+1


def test_derive_empty_trailing_iteration_produces_no_node():
    """末圈空采样(无 assistant_message)不留 item → 不产节点。"""
    hist = [_u(), _a(), _fc("c1"), _fco("c1")]  # 首圈派发,末圈空(无尾 assistant)
    nodes = derive_rewind_log(hist)
    assert [n.node_id for n in nodes] == ["t1:it1", "t1:disp0"]


def test_derive_multi_turn_addressable():
    """两 turn → t1 / t2 前缀分别可寻址。"""
    hist = [_u(), _a(), _u(), _a()]
    ids = [n.node_id for n in derive_rewind_log(hist)]
    assert ids == ["t1:it1", "t2:it1"]


def test_derive_multi_turn_dispatch_seq_restarts_per_turn():
    """每 turn 的 dispatch 序号从 0 重启:[u,a,fc, u,a,fc] → disp 为 t1:disp0 / t2:disp0。"""
    hist = [_u(), _a(), _fc("c1"), _u(), _a(), _fc("c2")]
    disps = [n.node_id for n in derive_rewind_log(hist) if n.kind == "dispatch"]
    assert disps == ["t1:disp0", "t2:disp0"]


def test_derive_ignores_unknown_kinds_for_index():
    """spawn 等未知 kind 计入下标、不产节点,后续下标不偏移。"""
    from taifeng.conversation.models import spawn_item
    sp = spawn_item(handle_id="h", skill_id="s", child_thread_id="c", thread_id=T)
    hist = [_u(), sp, _a(), _fc("c1"), _fco("c1")]
    nodes = derive_rewind_log(hist)
    it1 = next(n for n in nodes if n.node_id == "t1:it1")
    disp = next(n for n in nodes if n.node_id == "t1:disp0")
    assert it1.history_len == 2          # assistant_message 在 spawn 之后,下标 2
    assert disp.inner_history_len == 4   # fc 下标(3)+1


# ── Task 5：奇偶校验 derive ≡ live RewindLog ─────────────────────────────


async def _run_to_root_end(engine: object, text: str) -> None:
    """提交 user message 并消费事件到 root turn 终结，等待节点表落地。

    复用 test_turn_rewind.py 的 settle 模式：turn_completed 在
    runner.run() 内发出，engine 状态在 run() 返回后回写；轮询直到
    rewind_nodes() 非空才返回。
    """
    import asyncio

    import taifeng

    assert isinstance(engine, taifeng.AgentEngine)

    sub_id = await engine.submit(taifeng.UserMessage(text=text))

    # 消费事件到 root turn 终结（参照 test_turn_rewind._consume_to_root_end）
    async for ev in engine.subscribe_all():
        if ev.submission_id != sub_id:
            continue
        kind = ev.msg.kind
        if kind in ("turn_completed", "turn_failed"):
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if data.get("is_root"):
                break

    # 等节点表回写（engine 在 run() 返回后落 _rewind_checkpoints）
    for _ in range(100):
        if engine.rewind_nodes():
            break
        await asyncio.sleep(0.01)


async def test_parity_derive_equals_live_recording(
    skills_dir, threads_dir
) -> None:
    """奇偶校验：derive_rewind_log(history) ≡ 热路径 live RewindLog。

    构造一个包含多圈、多工具派发的热 turn：
    - 圈 1：一次 read_skill 派发
    - 圈 2：一次 read_skill 派发
    - 圈 3：无工具，收尾（带文本）

    热路径由 TurnRunner 现场 live 记录 rewind_log.checkpoints，
    turn 结束后 engine._rewind_checkpoints 回写（Task 8 前）。
    冷推导由 derive_rewind_log(engine.history_snapshot()) 独立推算。

    断言逐字段比较（除 cache_anchor 外，冷推导无 cache 信息置 -1）：
    - node_id、kind、turn_index
    - history_len、inner_history_len
    - iteration_index、call_id、target_id
    - args_digest（原始 args[:200]）

    若二者任一字段不一致，说明 derive 逻辑有 bug（此时报告首个分歧
    节点的 live vs derived 字段，禁止悄悄改 live 记录路径）。
    """
    import taifeng
    from taifeng.llm.providers import MockClient, MockTurn

    # 脚本覆盖：圈 1/2 各一次 read_skill，圈 3 无工具纯文本收尾
    client = MockClient(turns=[
        MockTurn(text="圈1推理", tool_calls=[
            {
                "id": "tc1",
                "name": "read_skill",
                "arguments": '{"skill_id":"style-checker"}',
            },
        ]),
        MockTurn(text="圈2推理", tool_calls=[
            {
                "id": "tc2",
                "name": "read_skill",
                "arguments": '{"skill_id":"style-checker"}',
            },
        ]),
        MockTurn(text="收尾推理"),  # 圈 3：无工具调用
    ])

    pool = await taifeng.EnginePool.create(
        skills_dir=skills_dir,
        threads_dir=threads_dir,
        model_client=client,
        compressors=[],
    )
    engine = await pool.get_or_create(
        session_id="s_parity", entry_skill_id="code-reviewer"
    )

    # 执行热 turn 并等待节点表落地
    await _run_to_root_end(engine, "请审查代码")

    # ── 取热路径 live 记录与冷推导结果 ────────────────────────────────────
    live = engine.rewind_nodes()           # 热路径 live 记录（Task 8 前）
    history = engine.history_snapshot()    # 当前 in-memory history
    derived = derive_rewind_log(history)   # 冷推导

    # 基本数量断言（5 节点：3 iteration + 2 dispatch）
    live_ids = [n.node_id for n in live]
    assert len(live) == 5, (
        f"热路径应有 5 个节点，实得 {len(live)}: {live_ids}"
    )
    derived_ids = [n.node_id for n in derived]
    assert len(derived) == len(live), (
        f"冷推导节点数({len(derived)}) ≠ 热路径({len(live)})\n"
        f"live     node_ids: {live_ids}\n"
        f"derived  node_ids: {derived_ids}"
    )

    # ── 逐字段奇偶校验 ──────────────────────────────────────────────────
    divergence_report: list[str] = []
    for i, (d, lv) in enumerate(zip(derived, live, strict=True)):
        # node_id 是核心定位键，必须完全一致
        if d.node_id != lv.node_id:
            divergence_report.append(
                f"[{i}] node_id 不一致: "
                f"derived={d.node_id!r} live={lv.node_id!r}"
            )
        # turn_index：turn 序号（1-based 累积 user_message 数）
        if d.turn_index != lv.turn_index:
            divergence_report.append(
                f"[{i}:{d.node_id}] turn_index 不一致: "
                f"derived={d.turn_index} live={lv.turn_index}"
            )
        # kind：iteration / dispatch
        if d.kind != lv.kind:
            divergence_report.append(
                f"[{i}:{d.node_id}] kind 不一致: "
                f"derived={d.kind!r} live={lv.kind!r}"
            )
        # history_len：re_reason 截断切点（下标）
        if d.history_len != lv.history_len:
            divergence_report.append(
                f"[{i}:{d.node_id}] history_len 不一致: "
                f"derived={d.history_len} live={lv.history_len}"
            )
        # inner_history_len：retry_tool 切点（仅 dispatch）
        if d.inner_history_len != lv.inner_history_len:
            divergence_report.append(
                f"[{i}:{d.node_id}] inner_history_len 不一致: "
                f"derived={d.inner_history_len} "
                f"live={lv.inner_history_len}"
            )
        # iteration_index：所属采样圈序号
        if d.iteration_index != lv.iteration_index:
            divergence_report.append(
                f"[{i}:{d.node_id}] iteration_index 不一致: "
                f"derived={d.iteration_index} "
                f"live={lv.iteration_index}"
            )
        # call_id：仅 dispatch
        if d.call_id != lv.call_id:
            divergence_report.append(
                f"[{i}:{d.node_id}] call_id 不一致: "
                f"derived={d.call_id!r} live={lv.call_id!r}"
            )
        # target_id：工具 / call_skill 名称
        if d.target_id != lv.target_id:
            divergence_report.append(
                f"[{i}:{d.node_id}] target_id 不一致: "
                f"derived={d.target_id!r} live={lv.target_id!r}"
            )
        # args_digest：原始 args[:200]
        if d.args_digest != lv.args_digest:
            divergence_report.append(
                f"[{i}:{d.node_id}] args_digest 不一致: "
                f"derived={d.args_digest!r} "
                f"live={lv.args_digest!r}"
            )
        # 注：cache_anchor 冷推导置 -1，热路径为真实值，
        # 故意跳过（非下标语义字段，不影响 rewind 正确性）

    # 若有分歧，打印完整报告再 fail（便于 controller 定位首个分歧）
    assert not divergence_report, (
        "derive_rewind_log 与 live RewindLog 出现分歧！\n"
        "首个分歧节点在报告第 1 条：\n"
        + "\n".join(divergence_report)
    )

    await pool.close()
