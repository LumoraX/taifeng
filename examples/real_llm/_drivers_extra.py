"""capability_matrix 新增能力的真实跑测剧本（ADR 0056 / 0059 / 0060 / 0061 / 0064 / 0065）。

与 ``_drivers.py`` 同一约定：``async def d(engine, res)`` 自行提交、轮询 ``res.events``
（subscribe_all 全量采集）推进。区别在于这里每个剧本都带**能区分「能力生效」与「没生效」
的断言**——不只是「turn 跑完了」：能力没生效时，断言里检查的那条证据不可能出现。
断言失败抛 ``AssertionError``，由矩阵主循环记为该场景 FAIL。

场景装配（工具 / 钩子 / 压缩器 / 预算）见 ``_setups.py``，token 与标记常量也从那里取。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from _drivers import _root_completions, _wait_for
from _setups import (
    COMPACT_CODE,
    COMPACT_CONSTRAINT,
    GUARD_CAP_BYTES,
    GUARD_HEAD_CODE,
    GUARD_INJECT_CODE,
    GUARD_INJECTION,
    GUARD_MID_CODE,
    GUARD_SANITIZED,
    GUARD_TAIL_CODE,
    GUARD_TOOL,
    INFERENCE_CHILD_MARK,
    INFERENCE_ENTRY_MARK,
    PINNED_FIRST_CODE,
    PINNED_HOST_CODE,
    READ_PATH_CODE,
    READ_PATH_FILE,
    READ_PATH_SKILL,
    SEARCH_CODE,
    SEARCH_TARGET,
)

import taifeng
from taifeng.context.pinned_state import pinned_injection_source
from taifeng.loop.submission import CompactNow
from taifeng.skill.registry import FilesystemSkillRegistry

if TYPE_CHECKING:
    from taifeng.loop.event import Msg

_SKILLS_EXTRA = Path(__file__).resolve().parent / "skills_extra"


# ── 通用取证工具 ───────────────────────────────────────────────────────────


def _requests(res: Any) -> list[dict[str, Any]]:
    """全部 ``llm_request_recorded`` 留痕（按实发顺序），即发往 provider 适配层的 ApiRequest。"""
    return [m.data for m in res.events if m.kind == "llm_request_recorded"]


def _blob(request: dict[str, Any]) -> str:
    """把一次请求序列化为可搜索文本（保留中文原样，便于子串断言）。"""
    return json.dumps(request, ensure_ascii=False)


def _answers(engine: Any) -> list[str]:
    """根 thread 历史里的全部 assistant 正文（按时间序）。"""
    return [
        str(it.payload.get("text", ""))
        for it in engine.history_snapshot()
        if it.kind == "assistant_message" and it.thread_id == engine.thread_id
    ]


def _last_answer(engine: Any) -> str:
    """最近一条根 thread assistant 正文；没有即断言失败（turn 至少要产出一句话）。"""
    answers = _answers(engine)
    assert answers, "根 thread 历史里没有任何 assistant 正文"
    return answers[-1]


def _tool_pairs(res: Any, name: str) -> list[tuple[Msg, Msg | None]]:
    """按 call_id 配对某工具的 (tool_call_started, tool_call_completed)。"""
    done = {m.data["call_id"]: m for m in res.events if m.kind == "tool_call_completed"}
    return [
        (m, done.get(m.data["call_id"]))
        for m in res.events
        if m.kind == "tool_call_started" and m.data["name"] == name
    ]


async def _run_turn(engine: Any, res: Any, text: str, nth: int) -> str:
    """提交一条用户消息，等第 ``nth`` 次 root 完成，返回本轮最终回答。"""
    await engine.submit(taifeng.UserMessage(text=text))
    await _wait_for(res, lambda m: _root_completions(res) >= nth,
                    what=f"第{nth}轮 root turn_completed", wait_seconds=240.0)
    return _last_answer(engine)


# ── skill_inference（ADR 0056）─────────────────────────────────────────────


async def _declared_inference(skill_id: str) -> tuple[str | None, int | None]:
    """从 SKILL.md 加载声明值（断言的期望值以 skill 文件为唯一真相源，不在剧本里重抄）。"""
    registry = await FilesystemSkillRegistry.load(_SKILLS_EXTRA / "skill_inference" / "skills")
    defn = registry.snapshot().get(skill_id)
    assert defn is not None, f"skill {skill_id} 未加载"
    return defn.inference.reasoning_effort, defn.inference.max_output_tokens


async def drive_skill_inference(engine: Any, res: Any) -> None:
    """entry 与经 call_skill 派发的子 skill 各按自己的 ``inference`` 块下发参数。

    区分点：能力未生效时请求里这两个字段恒为 None；若只按 client / 父 skill 一刀切，
    则父子两组请求的值相同。这里要求两组都非空、各等于各自声明、且两组声明互不相同。
    """
    entry_decl = await _declared_inference("release-coordinator")
    child_decl = await _declared_inference("change-classifier")
    assert None not in entry_decl and None not in child_decl, "SKILL.md 未声明完整 inference"
    assert entry_decl != child_decl, "父子声明相同则无法区分「各用各的」与「继承」"

    await _run_turn(engine, res, "请整理这三条变更：1. 修复登录页按钮错位；"
                    "2. 新增订单导出 CSV 功能；3. 升级日志组件依赖版本。", 1)
    entry_reqs: list[dict[str, Any]] = []
    child_reqs: list[dict[str, Any]] = []
    for req in _requests(res):
        system = "\n".join(req.get("system_prompt") or [])
        if INFERENCE_CHILD_MARK in system:
            child_reqs.append(req)
        elif INFERENCE_ENTRY_MARK in system:
            entry_reqs.append(req)
    assert entry_reqs, "没有捕获到 entry skill 的请求"
    assert child_reqs, "没有捕获到子 skill 的请求（模型未派发 change-classifier）"
    for label, reqs, decl in (("entry", entry_reqs, entry_decl), ("child", child_reqs, child_decl)):
        seen = {(r.get("reasoning_effort"), r.get("max_output_tokens")) for r in reqs}
        assert seen == {decl}, f"{label} 请求的推理参数 {seen} ≠ 声明 {decl}"
    print(f"  [inference] entry×{len(entry_reqs)}={entry_decl} "
          f"child×{len(child_reqs)}={child_decl}"
          "（codex wire：reasoning.effort / max_output_tokens）")


# ── tool_output_guard（ADR 0061）───────────────────────────────────────────


async def drive_tool_output_guard(engine: Any, res: Any) -> None:
    """PostToolUse 改写 + 结果字节上限：模型看到的是清洗、截断后的输出，且仍能据头尾作答。

    区分点：注入文本放在截断保留区（头部第 2 行），请求里看不到它只能归功于钩子改写；
    正中的 MID token 与截断标记则证明上限在入历史前生效。
    """
    answer = await _run_turn(engine, res, "请取回今天的巡检日志，然后告诉我日志第一行的 "
                             "HEAD-TOKEN 和最后一行的 TAIL-TOKEN 各是什么（原样给出）。", 1)
    pairs = _tool_pairs(res, GUARD_TOOL)
    assert pairs and pairs[0][1] is not None, f"模型未调用 {GUARD_TOOL} 或调用未完成"
    done = pairs[0][1].data
    assert done.get("output_rewritten_by_hook") is True, f"事件缺 output_rewritten_by_hook: {done}"
    capped = done.get("output_capped") or {}
    assert capped.get("cap_bytes") == GUARD_CAP_BYTES, f"事件缺 output_capped: {done}"
    assert capped.get("original_bytes", 0) > GUARD_CAP_BYTES, f"原始字节数未超上限: {capped}"

    # 工具之后的请求 = 含头部 token 的请求（用户消息里没有 token 值，只能来自工具输出）
    after_tool = [b for b in map(_blob, _requests(res)) if GUARD_HEAD_CODE in b]
    assert after_tool, "没有捕获到携带工具输出的后续请求"
    for blob in after_tool:
        assert GUARD_INJECTION not in blob and GUARD_INJECT_CODE not in blob, "注入文本进入了请求"
        assert GUARD_SANITIZED in blob, "请求里没有清洗占位（改写未进入模型视图）"
        assert f"exceeded the {GUARD_CAP_BYTES}-byte limit" in blob, "请求里没有截断标记"
        assert GUARD_MID_CODE not in blob, "正中内容未被截掉（上限未生效）"
    assert GUARD_HEAD_CODE in answer and GUARD_TAIL_CODE in answer, f"未复述头尾 token: {answer!r}"
    assert GUARD_INJECT_CODE not in answer, "模型回答里出现了注入 token"
    print(f"  [guard] rewritten=True capped={capped} "
          f"后续请求×{len(after_tool)} 均无注入、含截断标记")


# ── pinned_periodic（ADR 0065）─────────────────────────────────────────────


def _periodic_count(res: Any) -> int:
    """已观测到的 phase=periodic 周期重注次数。"""
    return sum(1 for m in res.events
               if m.kind == "pinned_state_reinjected" and m.data.get("phase") == "periodic")


async def drive_pinned_periodic(engine: Any, res: Any) -> None:
    """节奏 2 的周期重注：第 2、4 轮开头注入；第 4 轮模型复述只经重注才能看到的条目。

    区分点：第 2 轮后宿主直接改 store 追加 BADGE 条目——它从未出现在任何用户消息或工具
    结果里，模型第 4 轮能说出它，只能是周期重注把最新清单送进了上下文。每轮结束时的
    periodic 累计次数须为 [0, 1, 1, 2]（节奏本身也在断言内）；第 4 轮请求的最后一项须是
    含 BADGE 的 system 注入（尾部追加，内核侧证据）。

    第 4 轮只问「与门禁有关的那一项」而不是「列出最新清单」：codex wire 把中段 system
    上提进顶层 instructions，新旧两版清单并列、失去先后，真实模型会沿用对话里 todo_write
    输出的旧清单（首轮真实回归 2/2 次）。只问 BADGE 那一项，旧清单里没有可混淆的答案。
    """
    store = res.state["todo_store"]
    counts: list[int] = []
    await _run_turn(engine, res, "请用 todo_write 建立办公室搬迁准备清单，三项都为 pending："
                    f"① 盘点机柜设备（资产编号 {PINNED_FIRST_CODE}）② 预约搬运车辆 "
                    "③ 通知各部门搬迁时间。建好后只回复「已建立」。", 1)
    counts.append(_periodic_count(res))
    assert _tool_pairs(res, "todo_write"), "第 1 轮模型未调用 todo_write，清单为空无法验证重注"
    await _run_turn(engine, res, "车辆的事我来跟进。你先简单说一句：搬迁前一天最该确认什么？", 2)
    counts.append(_periodic_count(res))
    # 宿主侧追加一项（模拟其他参与方更新清单）：模型此后只能经周期重注得知
    store.replace([*store.items, {"content": f"领取门禁临时卡（凭证号 {PINNED_HOST_CODE}）",
                                  "status": "pending"}])
    await _run_turn(engine, res, "搬迁当天大概需要几个人手？一句话回答。", 3)
    counts.append(_periodic_count(res))
    answer = await _run_turn(engine, res, "不要调用工具。任务清单里有一项和门禁有关，"
                             "请把那一项原样写出来（括号里的内容也照抄）。", 4)
    counts.append(_periodic_count(res))
    assert counts == [0, 1, 1, 2], f"周期重注节奏不符：每轮累计 {counts}，期望 [0, 1, 1, 2]"
    injected = [it.payload.get("text", "") for it in engine.history_snapshot()
                if it.kind == "system_injection"
                and it.payload.get("source") == pinned_injection_source("todo")]
    assert injected and PINNED_HOST_CODE in injected[-1], "最近一次重注内容不含宿主追加的条目"
    last_req = _requests(res)[-1]
    tail = (last_req.get("input_items") or last_req.get("messages") or [{}])[-1]
    assert tail.get("role") == "system" and PINNED_HOST_CODE in str(tail.get("content")), (
        f"第 4 轮请求的最后一项不是含 {PINNED_HOST_CODE} 的重注: {tail}")
    assert PINNED_HOST_CODE in answer, f"第 4 轮未复述经重注送达的条目: {answer!r}"
    print(f"  [pinned] periodic 累计 {counts}；第 4 轮复述 {PINNED_HOST_CODE}")


# ── file_search（ADR 0064）─────────────────────────────────────────────────


async def drive_file_search(engine: Any, res: Any) -> None:
    """grep 按内容定位唯一文件。

    区分点：7 个文件里只有嵌套目录下的一个含 token，文件名不可猜；未注册 file_read，
    grep 是唯一能看到文件内容的途径。要求 grep 被调用、其输出命中目标文件、回答给出该路径。
    """
    answer = await _run_turn(engine, res, "工作目录里有一批分仓备忘。请找出哪个文件记录了"
                             f"调拨批次号 {SEARCH_CODE}，回答该文件相对工作目录的路径。", 1)
    greps = _tool_pairs(res, "grep")
    assert greps, "模型未调用 grep"
    hit = [done for _, done in greps if done is not None
           and not done.data["is_error"] and SEARCH_TARGET in done.data["output"]]
    assert hit, f"grep 输出未命中目标文件: {[d.data['output'] for _, d in greps if d]}"
    assert SEARCH_TARGET in answer, f"回答未给出正确文件 {SEARCH_TARGET}: {answer!r}"
    print(f"  [search] grep×{len(greps)} 命中 {SEARCH_TARGET}")


# ── read_skill_path（ADR 0060）─────────────────────────────────────────────


def _is_aux_read(started: Msg) -> bool:
    """该 read_skill 调用是否带 path 指向本场景的附属文件。"""
    args = json.loads(started.data["arguments"] or "{}")
    path = str(args.get("path") or "").removeprefix("./")
    return args.get("skill_id") == READ_PATH_SKILL and path == READ_PATH_FILE


async def drive_read_skill_path(engine: Any, res: Any) -> None:
    """正文只写「细节见 references/detail.md」，token 只在附属文件里。

    区分点：没有 path 能力时模型拿不到附属文件，回答不可能含 token；要求出现
    ``read_skill(skill_id, path)`` 调用、其输出含 token、回答复述 token。
    """
    answer = await _run_turn(engine, res, "季度盘点复核开始时，要向仓库主管报的复核口令是什么？"
                             "请查阅规程后原样告诉我。", 1)
    aux = [(s, d) for s, d in _tool_pairs(res, "read_skill") if _is_aux_read(s)]
    assert aux, f"模型未调用 read_skill(skill_id={READ_PATH_SKILL}, path={READ_PATH_FILE})"
    ok = [d for _, d in aux if d is not None and not d.data["is_error"]
          and READ_PATH_CODE in d.data["output"]]
    assert ok, f"附属文件读取失败或内容不含 token: {[d.data for _, d in aux if d]}"
    assert READ_PATH_CODE in answer, f"回答未复述附属文件里的 token: {answer!r}"
    print(f"  [read_skill] path 读取×{len(aux)}，回答含 {READ_PATH_CODE}")


# ── compaction_continuity（ADR 0059）───────────────────────────────────────


def _recent_user_block(summary: str) -> str:
    """取压缩条目里 ``<recent_user_messages>`` 段（不含则返回空串）。"""
    start = summary.find("<recent_user_messages>")
    end = summary.find("</recent_user_messages>")
    return summary[start:end] if 0 <= start < end else ""


async def drive_compaction_continuity(engine: Any, res: Any) -> None:
    """早期用户约束 → 强制压缩全部历史 → 压缩条目原样保留原话，压缩后模型仍守约束。

    区分点：``preserve_tail=0`` 把全部历史压进一个条目，原始 user_message 不再在历史里；
    约束原话须**逐字**出现在 ``<recent_user_messages>`` 段（LLM 摘要只会转述）。
    """
    await _run_turn(engine, res, COMPACT_CONSTRAINT, 1)
    await _run_turn(engine, res, "用两三句话说说数据备份的 3-2-1 原则是什么。", 2)
    await _run_turn(engine, res, "再用两三句话讲讲增量备份和全量备份的区别。", 3)
    await engine.submit(CompactNow(force=True, preserve_tail=0))
    done = await _wait_for(res, lambda m: m.kind == "compaction_completed",
                           what="compaction_completed", wait_seconds=240.0)
    assert done.data.get("success") is True, f"压缩失败: {done.data}"
    history = engine.history_snapshot()
    compacted = [it for it in history if it.kind == "compacted"]
    assert compacted, "压缩后历史里没有 compacted 条目"
    assert not any(it.kind == "user_message" and COMPACT_CODE in str(it.payload.get("text", ""))
                   for it in history), "约束原话仍以 user_message 留在历史里（未被压缩掉）"
    block = _recent_user_block(str(compacted[-1].payload.get("summary", "")))
    assert COMPACT_CONSTRAINT in block, "压缩条目的 <recent_user_messages> 未逐字保留约束原话"

    answer = await _run_turn(engine, res, "最后用一句话总结：个人电脑里的照片该怎么备份？", 4)
    assert COMPACT_CODE in answer, f"压缩后的回答未遵守约束（缺签名 {COMPACT_CODE}）: {answer!r}"
    print(f"  [compaction] 压缩移除 {done.data.get('removed_count')} 条；原话逐字保留；"
          f"压缩后回答含 {COMPACT_CODE}")


DRIVERS_EXTRA: dict[str, Any] = {
    "skill_inference": drive_skill_inference,
    "tool_output_guard": drive_tool_output_guard,
    "pinned_periodic": drive_pinned_periodic,
    "file_search": drive_file_search,
    "read_skill_path": drive_read_skill_path,
    "compaction_continuity": drive_compaction_continuity,
}
