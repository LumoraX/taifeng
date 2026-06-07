"""Taifeng Web UI demo —— 浏览器里实时看 agent 数据流 + HITL 审批。

支持多套 demo skill 包动态切换（顶部下拉），每套独立 EnginePool / 权限策略：

    - code_review     ：programmer ↔ code-review 派发，触发 HITL 弹窗
    - numeric_loop    ：LLM 自主多轮 run_script(apply_delta) 调谐

技术栈刻意保持最小：
    - FastAPI + Uvicorn（在 [dev] optional-deps，零生产影响）
    - SSE 推送事件（浏览器原生 EventSource，零客户端依赖）
    - CallbackPrompter SDK 模式（**不走 MCP**）

依赖：

    uv pip install -e ".[dev,litellm]"

运行：

    cd taifeng
    PYTHONPATH=src uv run python examples/web_ui/server.py
    # 浏览器开 http://localhost:8765

环境变量（多 provider 支持，native 优先）：

    # 通用形态（推荐）—— provider 决定走哪个 native client
    LLM_BOOTSTRAP_PROVIDER=openai|anthropic|gemini|deepseek  # 默认 openai
    LLM_BOOTSTRAP_API_KEY=...                                # provider 对应的 key
    LLM_BOOTSTRAP_MODEL=...                                  # 默认按 provider 给一个合理值
    LLM_BOOTSTRAP_BASE_URL=...                               # 可选，仅 openai-compat 网关需要

    # 旧形态（向后兼容）—— 等价于 PROVIDER=openai
    LLM_BOOTSTRAP_OPENAI_API_KEY=...
    LLM_BOOTSTRAP_OPENAI_MODEL=...
    LLM_BOOTSTRAP_OPENAI_BASE_URL=...

各 provider 默认模型：
    openai   → gpt-4o-mini                        （走 OpenAICompatClient）
    anthropic→ claude-haiku-4-5-20251001          （走 AnthropicClient）
    gemini   → gemini-2.0-flash-exp               （走 GeminiClient）
    deepseek → deepseek-chat                      （走 DeepSeekClient）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, StreamingResponse
    from pydantic import BaseModel
except ImportError as e:
    print(
        "缺少 fastapi/uvicorn 依赖，请先安装：\n"
        "    uv pip install -e \".[dev,litellm]\"\n"
        "或直接：uv pip install fastapi uvicorn\n",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

import taifeng
from taifeng.context.budget import ContextBudget
from taifeng.context.strategies.sliding import SlidingWindowStrategy
from taifeng.llm.client import ModelClient
from taifeng.telemetry.console import attach_console_sink
from taifeng.permission import (
    CallbackPrompter,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)
from taifeng.skill.scripts.python import PythonScriptExecutor
from taifeng.skill.scripts.shell import ShellScriptExecutor
from taifeng.tool.builtins.request_user_input import make_request_user_input_tool
from taifeng.tool.builtins.spawn_skill import (
    make_await_skills_tool,
    make_join_skill_tool,
    make_kill_skill_tool,
    make_spawn_skill_tool,
)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
STORAGE_DIR = HERE / ".runs"
STATIC_DIR = HERE / "static"

# 把 examples/ 目录加入 sys.path，让 _provider_bootstrap 可以 import
sys.path.insert(0, str(EXAMPLES_DIR))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)
# hooks_showcase demo 的业务钩子工厂（与该 demo 的 standalone demo.py 共用同一实现）
from hooks_showcase.hooks_lib import build_showcase_hook_runner  # noqa: E402
# mcp_showcase demo 的 MCP client 接线（spawn 外部 MCP server + 注册其工具）
from mcp_showcase.mcp_lib import connect_showcase_mcp  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("web_ui")


# ──────────────────────────────────────────────────────────────────
# Demo 注册表 —— 想加新 demo 直接在这里追加一项
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DemoMeta:
    """单个 demo 的元数据：UI 下拉显示 + lazy pool 创建参数。"""

    demo_id: str
    """URL-safe id，也作 storage / SSE 分组键。"""

    title: str
    """UI 下拉里的人类可读名字。"""

    description: str
    """一句话描述，UI 顶部展示。"""

    skills_dir: Path
    """skill 加载根目录。"""

    entry_skill_id: str
    """入口 skill 的 id（必须 entry: true）。"""

    sample_prompt: str
    """input 框 placeholder + 一键填入按钮。"""

    hitl_on_skill_dispatch: bool = True
    """True：call_skill 派发到非 read_ 子 skill 时弹 HITL；False：静默放行。"""

    permission_rules: tuple[PermissionRule, ...] = field(default_factory=tuple)
    """额外的权限规则（在内置规则前匹配）。"""

    policy_config_overrides: dict[str, Any] | None = None
    """Style A 配置覆盖。键可选 ``default_mode`` / ``deny`` / ``allow`` / ``ask``；
    list 字段为完整替换语义（不是 extend）。用于 permission_showcase 这种需要
    完全不同策略的 demo。None=用基础策略。"""

    context_window_override: int | None = None
    """覆盖 EnginePool 的 ContextBudget.context_window（tokens）。None=用 LLM
    实际窗口（来自 _llm_meta.context_window），仍 None 则用 ContextBudget 默认
    200_000。compression_showcase 设很小的值（如 1024）让压缩快速触发演示。"""

    use_sliding_compressor: bool = False
    """True 时注册 SlidingWindowStrategy 到 EnginePool.compressors。配合
    context_window_override 可在短对话内触发 ``compaction_started`` 事件。
    SlidingWindow 是兜底策略（不调 LLM，硬丢中段），适合 demo；生产推荐
    handoff（LLM 接力总结后丢中段，质量更高）。"""

    subagent_approval_mode: str | None = None
    """非 None 时，pool 的 ``DispatchPolicy`` 用该子 turn 审批模式
    （``inherit`` / ``auto_deny`` / ``auto_allow``）。配合 permission_policy，
    每次 call_skill 派发会 emit ``subagent_policy_overridden`` 供审计
    （subagent_isolation demo 用）。None=用默认 inherit。"""

    instruction_layers: tuple[Any, ...] = ()
    """注入 pool 的指令分层（``InstructionLayer``）；按 priority 合进 system_prompt。
    空=不注入（instructions demo 用静态层演示动态 system 指令）。"""

    hook_runner_factory: Callable[[], Any] | None = None
    """非 None 时，调用它构造一个 ``HookRunner`` 注入 pool（``hooks=``）。
    钩子是进程内 async 回调，可按运行时 args / 调用栈做动态决策（与声明式权限规则互补）。
    用工厂而非实例：每个 pool 拿独立 runner，避免跨 demo 共享可变注册表
    （hooks_showcase demo 用 pre/post_skill_dispatch 钩子演示按入参拦截）。"""

    mcp_connect: Callable[[], Any] | None = None
    """非 None 时为一个 async 工厂，返回 ``(McpStdioClient, list[ToolSpec])``：
    pool 创建时 spawn 外部 MCP server 子进程、把其工具作为 ``extra_tools`` 注入；
    client 存入 _mcp_clients，lifespan 收尾时统一 close（终止子进程）。
    （mcp_showcase demo 用，演示 taifeng 作为 MCP client 远程调用跨进程工具）。"""

    wants_user_input_tool: bool = False
    """True 时把 ``request_user_input`` 采集工具作为 ``extra_tools`` 注入 pool
    （opt-in，不默认全局注册）。配合声明 ``tool_names: [request_user_input]`` 的 skill
    实现表单型 HITL：调用即挂起 turn，前端据 ``response_schema`` 渲染表单，用户填写后
    经 ``/api/resume`` 提交 ``Resume(thread, {request_id: payload})`` 续跑。
    （form_hitl demo 用）。"""

    streams_detached: bool = False
    """True 时事件桥走 detached 分支：不按 submission_id 过滤（spawn 事件
    submission_id=handle_id 不被丢），退出谓词改为「根 turn 终态 ∧ 无存活 spawn ∧
    无未触发 barrier ∧ 无在跑 then_thread」；且 ``/api/resume`` 不再另起 bridge
    （chat bridge 仍存活，resume 续跑事件经它回流，避免重复推送）。
    detached-spawn / turn-rewind 这类「根 turn 完成后仍有异步活动」的 demo 用。"""

    wants_spawn_tools: bool = False
    """True 时把 detached-spawn 的 4 个工具（spawn_skill / await_skills /
    join_skill / kill_skill）作为 extra_tools 注入 pool，让 LLM 能并发分离发起
    子 skill（multi_expert_consult demo 用）。"""

    wants_rewind: bool = False
    """True 时前端在根 turn 完成后拉 ``/api/rewind_nodes`` 渲染回访节点表，
    支持点节点重跑（``/api/rewind``）。仅 turn_rewind demo 置 True。"""


DEMOS: dict[str, DemoMeta] = {
    "code_review": DemoMeta(
        demo_id="code_review",
        title="🔒 代码审查 (HITL 演示)",
        description="programmer ↔ code-review 双 skill 派发。call_skill 触发 HITL 审批弹窗。",
        skills_dir=EXAMPLES_DIR / "code_review" / "skills",
        entry_skill_id="programmer",
        sample_prompt=(
            "请审查这段代码：\n"
            "def login(username, password):\n"
            "    q = f\"SELECT * FROM users WHERE name='{username}' \"\n"
            "        f\"AND pwd='{password}'\"\n"
            "    return db.execute(q).fetchone()"
        ),
    ),
    "form_hitl": DemoMeta(
        demo_id="form_hitl",
        title="📝 表单 HITL (问答/单选/多选)",
        description=(
            "intake-coordinator 依次派发 questionnaire → summary。questionnaire 调 "
            "request_user_input 弹出结构化表单（问答题 + 单选 enum + 多选 array），用户填写后 "
            "Resume 续跑 → coordinator 继续派 summary 出小结。演示「表单型 HITL」+「子 skill "
            "挂起 → 用户输入 → 主 skill 继续派后续子 skill」。"
        ),
        skills_dir=EXAMPLES_DIR / "form_hitl" / "skills",
        entry_skill_id="intake-coordinator",
        sample_prompt="我来做个首诊，请帮我登记基础信息。",
        # 聚焦表单 HITL，关掉 call_skill 派发的权限审批弹窗（否则噪音）
        hitl_on_skill_dispatch=False,
        # opt-in 注入 request_user_input 采集工具
        wants_user_input_tool=True,
    ),
    "travel_planner": DemoMeta(
        demo_id="travel_planner",
        title="🧳 旅行规划 (fan-out + 综合)",
        description=(
            "trip-planner 入口 fan-out 三路 mock 查询（航班 / 酒店 / 活动），"
            "综合输出按日行程表 + 预算估算。multi-agent 最经典的并行收敛 pattern。"
        ),
        skills_dir=EXAMPLES_DIR / "travel_planner" / "skills",
        entry_skill_id="trip-planner",
        sample_prompt=(
            "帮我规划一次 9 月 12-15 日从北京到巴黎的 3 天旅行。\n"
            "- 出发: 北京\n"
            "- 目的地: 巴黎\n"
            "- 入住: 2026-09-12，退房: 2026-09-15\n"
            "- 人数: 2 人\n"
            "- 预算: ¥15000 人均\n"
            "- 兴趣: 美食、博物馆、夜景\n"
            "请按工作流程 fan-out 三个子 skill，最后给一份完整的按日行程方案。"
        ),
        # 三路 fan-out 都弹会噪音，关掉；HITL 演示交给 code_review demo
        hitl_on_skill_dispatch=False,
    ),
    "orchestration": DemoMeta(
        demo_id="orchestration",
        title="🧩 声明式编排 (parallel/serial/when)",
        description=(
            "trip-planner 用 SKILL.md 的 orchestration 声明确定性驱动子 skill："
            "两条线路并发 → 探测天气需求 → 按需查天气 → 汇总。与 travel_planner 的"
            "「LLM 自主 fan-out」对照——编排骨架由声明而非 LLM 临场决定（entry 不采样 LLM）。"
        ),
        skills_dir=EXAMPLES_DIR / "orchestration" / "skills",
        entry_skill_id="trip-planner",
        sample_prompt=(
            "帮我规划一次 3 天周末出游，给两条线路对比并按需提示天气。\n"
            "- 偏好：美食、轻徒步\n"
            "- 同行：2 人\n"
            "编排器会并发规划南北两线、判断是否需要天气、最后汇总推荐。"
        ),
        # 编排会派发多个子 skill（并发+串行），逐个弹 HITL 噪音大，关掉
        hitl_on_skill_dispatch=False,
    ),
    "concurrent_fanout": DemoMeta(
        demo_id="concurrent_fanout",
        title="⚡ 并发 fan-out (LLM 自主并行)",
        description=(
            "research-fanout 由 LLM 临场决定在一条消息里同时发起多个 call_skill，"
            "引擎并发派发（max_parallel_tool_calls + RwLock，call_skill 跳锁真并行）。"
            "与 orchestration 对照：并发由 LLM 决定，而非 SKILL.md 声明。"
        ),
        skills_dir=EXAMPLES_DIR / "concurrent_fanout" / "skills",
        entry_skill_id="research-fanout",
        sample_prompt=(
            "请就『城市夜间经济的利弊』做一次多源快速调研。\n"
            "可并发检索网络 / 学术 / 新闻三个源，最后综合成结论。"
        ),
        # 三路并发 fan-out，逐个弹 HITL 噪音大，关掉
        hitl_on_skill_dispatch=False,
    ),
    "read_skill_lazy": DemoMeta(
        demo_id="read_skill_lazy",
        title="📖 read_skill 懒加载 (skill-as-context)",
        description=(
            "knowledge-router 按需 read_skill 把子 skill 的 body 注入上下文（不派发子 turn）。"
            "子 skill 完整正文不预先进 prompt，只给 id+描述——LLM 用 read_skill 现取现用，省 token。"
        ),
        skills_dir=EXAMPLES_DIR / "read_skill_lazy" / "skills",
        entry_skill_id="knowledge-router",
        sample_prompt="我的登录接口怎么防 SQL 注入？（也可问正则 ReDoS）",
    ),
    "subagent_isolation": DemoMeta(
        demo_id="subagent_isolation",
        title="🛡️ 子 turn 隔离策略 (auto_deny)",
        description=(
            "programmer 派发 code-review 时，DispatchPolicy 的 subagent_approval_mode=auto_deny "
            "把子 turn 内的 ask 自动转 deny，并 emit subagent_policy_overridden 供审计。"
            "演示「子 agent 权限隔离」——父放行、子 turn 收紧。"
        ),
        skills_dir=EXAMPLES_DIR / "subagent_isolation" / "skills",
        entry_skill_id="programmer",
        sample_prompt=(
            "请审查并改进这段代码：\n"
            "def transfer(amount):\n"
            "    balance -= amount  # 没有校验余额/权限"
        ),
        # 子 turn 审批模式：auto_deny（无人值守保守默认）
        subagent_approval_mode="auto_deny",
        # 派发本身不弹 HITL，聚焦演示 subagent_policy_overridden 事件
        hitl_on_skill_dispatch=False,
    ),
    "instructions": DemoMeta(
        demo_id="instructions",
        title="📋 Instructions 注入 (动态 system 指令)",
        description=(
            "在 pool 注入一层静态 InstructionLayer（house-style），合进 system_prompt 影响"
            "全程输出风格——演示「指令分层注入」：业务侧在 skill body 之外叠加可热更的系统指令。"
        ),
        skills_dir=EXAMPLES_DIR / "code_review" / "skills",
        entry_skill_id="programmer",
        sample_prompt="请审查这段登录代码的安全性（注意观察输出是否遵循注入的 house-style 指令）。",
        hitl_on_skill_dispatch=False,
        instruction_layers=(
            taifeng.InstructionLayer(
                name="house-style",
                source=(
                    "【house-style 指令】全程用中文；每条结论必须标注严重性 P0/P1/P2 "
                    "与一句可执行修复建议；结尾用一行给出总体风险评级（高/中/低）。"
                ),
                scope="session",
                priority=10,
            ),
        ),
    ),
    "hooks_showcase": DemoMeta(
        demo_id="hooks_showcase",
        title="🪝 业务钩子拦截 (pre/post_skill_dispatch)",
        description=(
            "task-runner 派发 data-export 时，pre_skill_dispatch 钩子按*运行时 args* "
            "拦截高风险全量导出（scope=all → deny，emit skill_dispatch_hook_denied），"
            "改 scope=recent 才放行；post_skill_dispatch 钩子事后审计（不可否决）。"
            "演示「钩子 = 命令式业务回调」与声明式权限规则的互补——后者读不到本次入参。"
        ),
        skills_dir=EXAMPLES_DIR / "hooks_showcase" / "skills",
        entry_skill_id="task-runner",
        sample_prompt=(
            "请帮我导出数据，优先全量导出；若被风控拦截，则退而求其次导出近期数据，"
            "最后告诉我实际导出了哪一档。"
        ),
        # 派发本身不弹 HITL，聚焦演示钩子的 skill_dispatch_hook_denied 事件
        hitl_on_skill_dispatch=False,
        # 注入业务钩子工厂：pre（可否决，按 args 拦截）+ post（仅审计）
        hook_runner_factory=build_showcase_hook_runner,
    ),
    "mcp_showcase": DemoMeta(
        demo_id="mcp_showcase",
        title="🔌 MCP 工具 (跨进程远程调用)",
        description=(
            "market-assistant 调用的 lookup_stock_price / convert_currency 不是内置工具、"
            "也不是 skill —— 它们来自一个独立的 MCP server 子进程：taifeng 作为 MCP client "
            "spawn 它、握手、tools/list 后注册成普通工具。infra 四类里唯一真·跨进程的体现，"
            "工具调用/结果仍走统一事件流（时间轴 + 可观测面板可见）。"
        ),
        skills_dir=EXAMPLES_DIR / "mcp_showcase" / "skills",
        entry_skill_id="market-assistant",
        sample_prompt=(
            "帮我查一下 AAPL 的股价，并按汇率把它折算成人民币；也可以试试 TSLA / NVDA。"
        ),
        # MCP 工具是远程调用，逐个弹 HITL 噪音大；聚焦演示「工具来自子进程」本身
        hitl_on_skill_dispatch=False,
        # 注入 MCP 接线：pool 创建时 spawn server 子进程 + 注册工具
        mcp_connect=connect_showcase_mcp,
    ),
    "research_assistant": DemoMeta(
        demo_id="research_assistant",
        title="🔬 深度调研 (sequential pipeline)",
        description=(
            "research-lead 按 ①→②→③ 严格串行：source-collector → fact-extractor → "
            "report-writer。演示 sequential dependency，上步输出 = 下步输入。"
        ),
        skills_dir=EXAMPLES_DIR / "research_assistant" / "skills",
        entry_skill_id="research-lead",
        sample_prompt=(
            "请围绕「AI agent 框架的市场格局与瓶颈」做一次调研。\n"
            "请严格按照工作流程执行三步：\n"
            "① 调 source-collector 采集候选来源（max_sources=6）\n"
            "② 把 source-collector 返回的 candidates JSON 字符串原样\n"
            "  转发给 fact-extractor 做事实提炼\n"
            "③ 把 fact-extractor 返回的 facts JSON 字符串原样\n"
            "  转发给 report-writer 生成 outline\n"
            "最后综合三段输出一份完整调研报告。"
        ),
        hitl_on_skill_dispatch=False,
    ),
    "product_review": DemoMeta(
        demo_id="product_review",
        title="📋 产品评审 (fan-out + 评分聚合)",
        description=(
            "product-manager fan-out 三个 reviewer（设计 / 工程 / 测试），"
            "聚合评分后按规则输出通过 / 修改 / 驳回结论 + 优先级修改清单。"
            "开 HITL：调 reviewer 是有成本的操作，需审批。"
        ),
        skills_dir=EXAMPLES_DIR / "product_review" / "skills",
        entry_skill_id="product-manager",
        sample_prompt=(
            "请评审以下 PRD：\n\n"
            "[需求名] 跨端实时购物车同步\n"
            "[目标] 支持用户在 H5 / iOS / Android 三端实时看到同一份购物车，"
            "涉及并发更新与事务一致性。支持灰度发布，按城市百分比放量。\n"
            "[范围] 加购 / 减购 / 删除 / 选中状态同步。\n"
            "[非目标] 离线编辑、商品推荐、订单转化。\n"
            "请按工作流程 fan-out 三个 reviewer，最后给评审纪要 + 通过 / 驳回结论。"
        ),
        # 评审 reviewer = 占用人力，每次派发都弹审批语义自然
        hitl_on_skill_dispatch=True,
    ),
    "numeric_loop": DemoMeta(
        demo_id="numeric_loop",
        title="🎯 数值调谐 (震荡回归)",
        description="LLM 自主多轮调 run_script(apply_delta) 把 current 调到 target ±0.5。",
        skills_dir=EXAMPLES_DIR / "numeric_loop" / "skills",
        entry_skill_id="numeric-tuner",
        sample_prompt=(
            "请把 current 调谐到 target。\n"
            "- current = 10\n"
            "- target  = 50\n"
            "严格按工作流执行，反复调 apply_delta 直到 gap < 0.5 或 12 轮上限。\n"
            "最后给【调谐报告】，包含完整轨迹列表。"
        ),
        # numeric_loop 的工具调用本质是同一 skill 内的 run_script，不涉及子 skill
        # 派发；如果开启 HITL 会被弹 12 次太吵 —— 关掉 skill_dispatch 询问
        hitl_on_skill_dispatch=False,
    ),
    "multi_expert_consult": DemoMeta(
        demo_id="multi_expert_consult",
        title="🩺 多专家会诊 (并发 spawn + 错峰 HITL + 联合会诊)",
        description=(
            "orchestrator 一个 turn 内对多个专科 spawn_skill（各自 detached child "
            "thread），await_skills 登记 join-barrier；各专家错峰 HITL，全终态 → "
            "barrier 自动起 joint-consult 聚合。演示 detached-spawn 完整闭环。"
        ),
        skills_dir=EXAMPLES_DIR / "multi_expert_consult" / "skills",
        entry_skill_id="orchestrator",
        sample_prompt="我最近血压偏高、体重也涨了，帮我看看身体情况。",
        hitl_on_skill_dispatch=False,
        streams_detached=True,
        wants_spawn_tools=True,
        wants_user_input_tool=True,
    ),
    "compression_showcase": DemoMeta(
        demo_id="compression_showcase",
        title="🗜️ 上下文压缩演示 (1k 窗口)",
        description=(
            "故意用 1024 token 的极小 context window + SlidingWindowStrategy；"
            "聊 2-3 轮就会撞上 hard_limit，时间轴出现 compaction_started / "
            "compaction_completed，history 中段被丢弃 + 写入 placeholder。"
        ),
        skills_dir=EXAMPLES_DIR / "compression_showcase" / "skills",
        entry_skill_id="chatty-assistant",
        sample_prompt=(
            "请详细讲讲 Python 装饰器是怎么工作的，举几个例子，并比较一下\n"
            "和 Java 注解的区别。然后我会问你下一个问题继续撑大上下文。"
        ),
        hitl_on_skill_dispatch=False,
        # 关键差异：用 1024 token 极小窗口 + sliding 压缩
        context_window_override=1024,
        use_sliding_compressor=True,
    ),
    "selective_approval": DemoMeta(
        demo_id="selective_approval",
        title="🔐 差异化授权 (按 skill 精细策略)",
        description=(
            "analysis-orchestrator 同时派发 prd-evaluator（白名单 allow）+ "
            "swot-evaluator（ask 弹窗）。演示同一 turn 内不同 skill 受不同 "
            "policy 约束 —— 生产中最常用的中间地带模式。"
        ),
        skills_dir=EXAMPLES_DIR / "selective_approval" / "skills",
        entry_skill_id="analysis-orchestrator",
        sample_prompt=(
            "请帮我同时做 PRD 评估 + SWOT 战略分析：\n\n"
            "[方案] 我们计划做一款 AI 编程助手，自研模型 + VSCode 多端集成，"
            "瞄准实时辅助编程的新兴市场。\n"
            "[团队] 5 年 AI 工程经验，6 人小团队，自有专利 3 项。\n"
            "[目标] 12 个月内 1 万 DAU；订阅制商业模式。\n"
            "[范围] 代码补全、错误诊断、单元测试自动生成。\n"
            "[非目标] 不做 IDE 本身、不做企业部署版。\n"
            "[挑战] 大厂同类产品已就位、模型推理成本高、监管不确定。\n\n"
            "请按工作流程 fan-out 两个子 skill：\n"
            "  - prd-evaluator（白名单 allow，会静默执行）\n"
            "  - swot-evaluator（ask，会弹审批窗口请你点允许）\n"
            "最后整合成一份分析报告。"
        ),
        # 关掉 hitl_on_skill_dispatch 的自动注入 Skill(re:...) ask 规则；
        # 完全由 policy_config_overrides 控制每个子 skill 的 mode
        hitl_on_skill_dispatch=False,
        policy_config_overrides={
            "default_mode": "allow",
            "deny": [
                "Bash(re:^rm\\s+-rf\\s+/)",
                "Bash(re:^sudo\\b)",
            ],
            "allow": [
                "FileRead(*)",
                "FileWrite(/tmp/*)",
                "Skill(prd-evaluator)",  # ← 这个子 skill 静默放行
                "Skill(read_*)",
            ],
            "ask": [
                "Skill(swot-evaluator)",  # ← 这个子 skill 必须弹审批
            ],
        },
    ),
    "permission_showcase": DemoMeta(
        demo_id="permission_showcase",
        # 这个 demo 主打"策略卡片直接可读 + skill 派发默认通过"
        title="🛡️ 权限策略演示 (全通过 + 红线 deny)",
        description=(
            "default_mode=allow + skill 派发默认通过，HITL 关闭；"
            "全局 deny 兜底拦截 rm -rf / sudo。"
            "演示 'allow Skill(*) + deny 危险命令' 的最小骨架。"
        ),
        # 复用 code_review 的双 skill (programmer ↔ code-review)，能直接演示
        # 子 skill 派发"无声通过"——和 code_review demo 对比 HITL 弹窗的差异
        skills_dir=EXAMPLES_DIR / "code_review" / "skills",
        entry_skill_id="programmer",
        sample_prompt=(
            "请审查这段代码（演示 skill 派发不弹窗的全通过策略）：\n"
            "def parse_input(s):\n"
            "    return eval(s)  # 危险\n"
        ),
        hitl_on_skill_dispatch=False,
        # 覆盖基础 policy：default 改 allow，deny 只保留两条最经典红线
        policy_config_overrides={
            "default_mode": "allow",
            "deny": [
                "Bash(re:^rm\\s+-rf\\b)",   # rm -rf 任何路径都拒
                "Bash(re:^sudo\\b)",         # sudo 一律拒
            ],
            "allow": [],   # 不写 allow，全靠 default_mode=allow 兜底
            "ask": [],
        },
    ),
}


# ──────────────────────────────────────────────────────────────────
# 全局状态：每 demo 独立 pool + 待审批 HITL future 表 + SSE 订阅者
# ──────────────────────────────────────────────────────────────────


_pools: dict[str, taifeng.EnginePool] = {}

_mcp_clients: dict[str, Any] = {}
"""demo_id → McpStdioClient（仅 mcp_connect demo 有）。lifespan 收尾时统一 close
以终止 MCP server 子进程；与 _pools 同生命周期。"""
"""demo_id → EnginePool，lazy 创建。"""

_pools_lock = asyncio.Lock()

_pending_hitl: dict[str, asyncio.Future[PermissionDecision]] = {}
"""hitl request_id → future。前端 POST /api/hitl/{id} 时 set_result。"""

_consoled_engines: set[int] = set()
"""已挂 ConsoleSink 的 AgentEngine id 集合 —— 同一 engine 不重复挂订阅。
用 id() 而非引用以避免 set 阻止 engine GC。"""

_event_subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
"""SSE 订阅键 = f"{demo_id}:{session_id}"。同一 demo 跨 session 的订阅者会收到本 demo
内任何 session 触发的 HITL（首响应者点了就关）。"""

_shutdown_event: asyncio.Event | None = None
"""进程级关停信号。lifespan 收尾时 set，长连接 SSE 生成器据此**主动**退出，
避免 uvicorn 优雅关停超时后强杀 → 抛 CancelledError 噪声栈。首次在事件循环内惰性创建。"""


def _get_shutdown_event() -> asyncio.Event:
    """惰性创建关停 Event（确保绑定到运行中的事件循环）。"""
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event

_model_client: ModelClient | None = None
# build_model_client 返回的 meta（provider / model / context_window 等）；
# /api/demos 通过它把 LLM 元信息透给前端，让 pill 能渲染 "ctx 18.5k / 128k"。
_llm_meta: dict[str, Any] = {}


# ──────────────────────────────────────────────────────────────────
# HITL prompter
# ──────────────────────────────────────────────────────────────────


def _make_prompter(demo_id: str) -> CallbackPrompter:
    """为每个 demo 创建一个独立 prompter，闭包绑定 demo_id 让事件只推到该 demo 订阅者。"""

    async def callback(req: PermissionRequest) -> PermissionDecision:
        request_id = f"hitl_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[PermissionDecision] = loop.create_future()
        _pending_hitl[request_id] = fut

        # call-skill-reason-field 之后 call_skill schema 已支持 reason 字段，
        # 但 LLM 仍可选择不填（tool description 是"建议"而非"必须"）。
        # 此时合成一个上下文摘要供审批方决策 —— 不假装是 LLM 的理由。
        depth = len(req.call_chain) + 1
        caller = req.metadata.get("caller_skill_id") if req.metadata else None
        args_brief = ""
        if req.metadata and isinstance(req.metadata.get("args"), dict):
            try:
                args_brief = json.dumps(
                    req.metadata["args"], ensure_ascii=False,
                )[:200]
            except (TypeError, ValueError):
                args_brief = ""
        if req.scope == "skill_dispatch":
            synth = (
                f"caller={caller or req.entry_skill_id} → "
                f"target={req.target} (depth={depth})"
            )
        elif req.scope == "tool_use":
            synth = (
                f"tool={req.target}"
                + (f"  args={args_brief}" if args_brief else "")
            )
        else:
            synth = f"scope={req.scope} target={req.target}"

        payload = {
            "kind": "hitl_required",
            "request_id": request_id,
            "demo_id": demo_id,
            "scope": req.scope,
            "target": req.target,
            "reason": req.reason,
            "synthesized_reason": synth,
            "thread_id": req.thread_id,
            "entry_skill_id": req.entry_skill_id,
            "call_chain": list(req.call_chain),
            "depth": depth,
            "metadata": req.metadata,
        }
        # 推到本 demo 所有 session 订阅者
        for sub_key, queues in _event_subs.items():
            if sub_key.startswith(f"{demo_id}:"):
                for q in queues:
                    q.put_nowait(payload)
        logger.info(
            "HITL prompt demo=%s rid=%s scope=%s target=%s",
            demo_id, request_id, req.scope, req.target,
        )
        try:
            return await fut
        finally:
            _pending_hitl.pop(request_id, None)

    return CallbackPrompter(callback)


def _build_policy_config(meta: DemoMeta) -> dict[str, Any]:
    """构造 PermissionPolicy Style A 配置 dict —— 纯函数，无副作用。

    抽出来的目的：``_make_policy`` 实例化 policy 用，``/api/demos`` 透传给前端
    渲染权限卡片也用，两者共享同一配置。

    Returns:
        Style A 配置：``{default_mode, deny[], ask[], allow[]}``，可直接喂给
        ``PermissionPolicy.from_dict``，也可序列化给前端展示。
    """
    # 共享基础策略（所有 demo 适用的危险命令拦截）
    config: dict[str, Any] = {
        "default_mode": "allow",
        "deny": [
            "Bash(re:^rm\\s+-rf\\s+/)",     # rm -rf / 或 /xxx 拦截
            "Bash(re:^sudo\\b)",             # sudo 一律拒
            "Bash(re:>\\s*/etc/)",           # 重定向到 /etc/ 拒
        ],
        "ask": [],
        "allow": [
            "FileRead(*)",
            "FileWrite(/tmp/*)",
        ],
    }
    if meta.hitl_on_skill_dispatch:
        # call_skill 派发到非 read_* 的子 skill → 走 HITL
        config["ask"].append("Skill(re:^(?!read_).+)")
        config["allow"].append("Skill(read_*)")
    # else（如 numeric_loop）：不加 Skill 规则，子 skill 派发由 default_mode=allow 兜底

    # demo 自定义覆盖：permission_showcase 这种主打"全通过"的 demo 需要更激进配置
    overrides = meta.policy_config_overrides
    if overrides:
        # default_mode 直接覆盖；list 字段 extend（覆盖语义在业务侧）
        if "default_mode" in overrides:
            config["default_mode"] = overrides["default_mode"]
        for key in ("deny", "allow", "ask"):
            if key in overrides:
                config[key] = list(overrides[key])  # 完整替换，保留语义清晰
    return config


def _make_policy(meta: DemoMeta) -> PermissionPolicy:
    """按 demo 元数据组装权限策略 —— 演示 PermissionPolicy.from_dict 的两种用法。

    Style A（语法糖）：用 ``Bash(...)`` / ``Skill(...)`` 字符串声明规则。
    Style B（明文）：直接列出 dict 形式的 PermissionRule（业务侧也可这样）。

    本 demo 用 Style A —— 给所有 demo 注入"安全 shell 白名单 + 危险命令拦截"，
    具体规则见 ``_build_policy_config``。
    """
    config = _build_policy_config(meta)
    policy = PermissionPolicy.from_dict(
        config,
        prompter=_make_prompter(meta.demo_id),
        prompter_timeout_seconds=300.0,
    )
    # DemoMeta.permission_rules 是业务侧可插入的额外规则（demo 默认空）；
    # 直接 append 到 policy.rules 末尾（仅在所有 Style A 规则之后兜底）
    policy.rules.extend(meta.permission_rules)
    return policy


async def _get_or_create_pool(demo_id: str) -> taifeng.EnginePool:
    """按需创建 demo 对应的 EnginePool（首次 chat 时触发）。"""
    if demo_id in _pools:
        return _pools[demo_id]
    if demo_id not in DEMOS:
        raise HTTPException(404, f"unknown demo: {demo_id}")
    meta = DEMOS[demo_id]
    if not meta.skills_dir.is_dir():
        raise HTTPException(500, f"skills_dir missing: {meta.skills_dir}")
    if _model_client is None:
        raise HTTPException(503, "model client not initialized")

    async with _pools_lock:
        # 二次检查（避免并发首请求重复建 pool）
        if demo_id in _pools:
            return _pools[demo_id]

        demo_storage = STORAGE_DIR / demo_id
        demo_storage.mkdir(parents=True, exist_ok=True)
        policy = _make_policy(meta)
        # budget 决策三档优先级：
        #   1. demo 显式 override（compression_showcase 设 1024 触发压缩）
        #   2. _llm_meta.context_window（按 model 真实窗口；UI ctx 占比也用它）
        #   3. ContextBudget 默认 200_000（保守兜底）
        ctx_win = (
            meta.context_window_override
            or _llm_meta.get("context_window")
            or 200_000
        )
        budget = ContextBudget(context_window=ctx_win)
        # 压缩器列表：默认空（不压缩）；demo 显式启用 sliding 时挂上
        compressors: list[Any] = []
        if meta.use_sliding_compressor:
            # keep_tail=2 是兜底策略下能保留的最少尾部消息数。SkillDispatch
            # 配对安全边界（_walk_back_to_safe_boundary）会自动往前调整。
            compressors.append(SlidingWindowStrategy(keep_tail=2))
        # 可选：子 turn 审批模式（subagent_isolation demo）——非 None 才构造 DispatchPolicy
        dispatch_policy = (
            taifeng.DispatchPolicy(
                subagent_approval_mode=meta.subagent_approval_mode,  # type: ignore[arg-type]
            )
            if meta.subagent_approval_mode is not None
            else None
        )
        # 可选：业务钩子（hooks_showcase demo）——每个 pool 调工厂拿独立 HookRunner
        hooks = (
            meta.hook_runner_factory()
            if meta.hook_runner_factory is not None
            else None
        )
        # 可选：MCP（mcp_showcase demo）——spawn 外部 MCP server 子进程并注册其工具，
        # 作为 extra_tools 注入；client 存入 _mcp_clients，收尾时 close。
        extra_tools: list[Any] = []
        if meta.mcp_connect is not None:
            mcp_client, mcp_specs = await meta.mcp_connect()
            extra_tools.extend(mcp_specs)
            _mcp_clients[demo_id] = mcp_client
            logger.info(
                "MCP 连接：demo=%s server=%s tools=%s",
                demo_id, mcp_client.server_info.get("name"),
                [s.name for s in mcp_specs],
            )
        # opt-in 注入表单采集工具（form_hitl demo）：声明 tool_names 的 skill 才会用到
        if meta.wants_user_input_tool:
            extra_tools.append(make_request_user_input_tool())
        # opt-in 注入 detached-spawn 四工具（multi_expert_consult demo）
        if meta.wants_spawn_tools:
            extra_tools.extend([
                make_spawn_skill_tool(),
                make_await_skills_tool(),
                make_join_skill_tool(),
                make_kill_skill_tool(),
            ])
        pool = await taifeng.EnginePool.create(
            skills_dir=meta.skills_dir,
            storage_dir=demo_storage,
            model_client=_model_client,
            budget=budget,
            compressors=compressors,
            max_iterations=30,
            script_executors={
                "shell": ShellScriptExecutor(),
                "python": PythonScriptExecutor(),
            },
            permission_policy=policy,
            dispatch_policy=dispatch_policy,
            # 可选：静态/动态指令分层（instructions demo）；空元组 → None=不注入
            instruction_layers=list(meta.instruction_layers) or None,
            # 可选：业务钩子（hooks_showcase demo）；None=不注入
            hooks=hooks,
            # 可选：MCP 远程工具 / request_user_input 采集工具；空列表→None=无
            extra_tools=extra_tools or None,
        )
        _pools[demo_id] = pool
        logger.info("EnginePool 创建：demo=%s skills_dir=%s", demo_id, meta.skills_dir)
        return pool


# ──────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────


def _register_external_skills_demos() -> None:
    """按环境变量把一个**外部 skills 目录**里的每个 entry skill 注册成 demo。

    用途：验证业务侧自有 skill（无需改代码、不写死绝对路径）。用法——

        TAIFENG_WEBUI_EXTRA_SKILLS_DIR=/abs/path/to/agent_skills \\
        PYTHONPATH=src uv run python examples/web_ui/server.py

    可选 ``TAIFENG_WEBUI_EXTRA_ENTRY=id1,id2`` 只注册指定 entry（逗号分隔）；缺省则
    该目录下所有 ``entry: true`` 的 skill 各注册一个 demo（id = ``ext_<skill_id>``）。
    外部 demo 默认注入 ``request_user_input`` 工具 + 关闭 call_skill 派发审批弹窗，
    聚焦「表单型 HITL + 多步 skill 链路」验证。
    """
    raw = os.getenv("TAIFENG_WEBUI_EXTRA_SKILLS_DIR")
    if not raw:
        return
    skills_dir = Path(raw).expanduser().resolve()
    if not skills_dir.is_dir():
        logger.warning("TAIFENG_WEBUI_EXTRA_SKILLS_DIR 非目录，跳过: %s", skills_dir)
        return
    from taifeng.skill.loader import load_skills_from_dir
    try:
        skills = load_skills_from_dir(skills_dir)
    except Exception as exc:  # noqa: BLE001 —— demo 容错：加载失败仅跳过，不拖垮启动
        logger.warning("外部 skills 加载失败，跳过: %s (%s)", skills_dir, exc)
        return
    only = os.getenv("TAIFENG_WEBUI_EXTRA_ENTRY")
    only_ids = {s.strip() for s in only.split(",")} if only else None
    entries = [
        s for s in skills.values()
        if s.entry and (only_ids is None or s.id in only_ids)
    ]
    for s in sorted(entries, key=lambda x: x.id):
        did = f"ext_{s.id}"
        DEMOS[did] = DemoMeta(
            demo_id=did,
            title=f"🧩 {s.id}（外部）",
            description=(s.description or f"外部 skill {s.id}")[:200],
            skills_dir=skills_dir,
            entry_skill_id=s.id,
            sample_prompt="（业务侧自有 skill）输入一段能触发该 skill 链路的内容。",
            hitl_on_skill_dispatch=False,
            wants_user_input_tool=True,
        )
        logger.info("注册外部 demo: %s (entry=%s dir=%s)", did, s.id, skills_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model_client, _llm_meta
    # 加载 .env（推迟到 lifespan 让 module level import 干净）
    load_dotenv_files()
    # 外部 skills demo（env 驱动）—— 在 .env 加载后注册，业务侧 skill 即可在 UI 选中
    _register_external_skills_demos()

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    # web_ui 启动时 api_key 可能没设；require_api_key=False 让构造继续，
    # LLM 调用时再失败（便于 demo 调试 SSE / UI 路径）
    try:
        _model_client, _meta = build_model_client(require_api_key=False)
        _llm_meta = dict(_meta)
        logger.info(
            "model client 就绪 (provider=%s model=%s base=%s ctx=%s)；"
            "demo 注册数=%d",
            _meta["provider"], _meta["model"], _meta.get("base_url", "-"),
            _meta.get("context_window", "?"), len(DEMOS),
        )
    except ProviderBootstrapError as exc:
        logger.warning("model client 构造失败: %s", exc)
        _model_client = None
        _llm_meta = {}
    try:
        yield
    finally:
        # 先广播关停：让所有长连接 SSE 生成器主动退出（在 timeout_graceful_shutdown 内），
        # 避免被 uvicorn 强杀抛 CancelledError 噪声栈。
        _get_shutdown_event().set()
        for demo_id, pool in list(_pools.items()):
            try:
                await pool.close()
            except Exception:
                logger.exception("pool close failed: demo=%s", demo_id)
            _pools.pop(demo_id, None)
        # 关闭 MCP client = 终止其 server 子进程（pool 关完再关，避免还有 in-flight 调用）
        for demo_id, client in list(_mcp_clients.items()):
            try:
                await client.close()
            except Exception:
                logger.exception("mcp client close failed: demo=%s", demo_id)
            _mcp_clients.pop(demo_id, None)


app = FastAPI(title="Taifeng Web UI Demo", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    demo_id: str = "code_review"
    session_id: str = "default"
    resume_thread_id: str | None = None
    """非 None 时续接已持久化的 thread（R5 resume）：首次发到该 session 的消息会从
    JSONL 物化历史构造 engine（kernel get_or_create 的 resume_thread_id 路径），
    后续发同一 session 命中缓存 engine。前端续聊时把 session 设为 ``resume:<tid>``
    并带上本字段。"""


class HitlDecisionRequest(BaseModel):
    granted: bool
    reason: str = "user_decision"


class ResumeFormRequest(BaseModel):
    """表单型 HITL 续跑：用户填完 request_user_input 弹出的表单后提交。"""

    demo_id: str
    session_id: str = "default"
    thread_id: str
    """挂起所在 thread（request_user_input 落在子 skill 的子 thread）。"""
    request_id: str
    """待回填的 pending request_id（= 该 request_user_input call 的 call_id）。"""
    payload: dict[str, Any]
    """用户填写的表单答案，直接成为该 call 的 function_call_output。"""


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/demos")
async def list_demos() -> dict[str, Any]:
    """前端拿来填下拉列表 + 渲染权限策略卡片。

    ``policy_preview`` 字段直接复用 Style A 配置 dict（``_build_policy_config``
    的输出），前端按 deny / allow / ask 三组三色 chip 渲染；不含业务侧追加的
    ``meta.permission_rules``（默认空，需要时另出字段）。
    """
    # llm 字段：所有 demo 共用同一个 ModelClient，meta 放顶层而不是每条
    # demo 重复。前端拿到 context_window 后按比例渲染 "ctx N.Nk / Mk (X%)"。
    return {
        "llm": {
            "provider": _llm_meta.get("provider"),
            "model": _llm_meta.get("model"),
            "context_window": _llm_meta.get("context_window"),
        },
        "demos": [
            {
                "demo_id": m.demo_id,
                "title": m.title,
                "description": m.description,
                "entry_skill_id": m.entry_skill_id,
                "sample_prompt": m.sample_prompt,
                "hitl_on_skill_dispatch": m.hitl_on_skill_dispatch,
                "policy_preview": _build_policy_config(m),
                "loaded": m.demo_id in _pools,
                "streams_detached": m.streams_detached,
                "wants_rewind": m.wants_rewind,
            }
            for m in DEMOS.values()
        ],
    }


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    if req.demo_id not in DEMOS:
        raise HTTPException(404, f"unknown demo_id: {req.demo_id}")
    meta = DEMOS[req.demo_id]
    pool = await _get_or_create_pool(req.demo_id)
    # 用 demo_id 隔离 session_id，避免 code_review 的 session "default" 误用
    # numeric_loop 的 entry skill
    pool_session_id = f"{req.demo_id}:{req.session_id}"
    # resume：首次发到该 session 时从持久化 thread 物化历史；session 已有缓存
    # engine 则 kernel 忽略本参数（继续既有 thread）——见 get_or_create docstring。
    engine = await pool.get_or_create(
        session_id=pool_session_id,
        entry_skill_id=meta.entry_skill_id,
        resume_thread_id=req.resume_thread_id,
    )
    # 首次接触此 engine 时挂 ConsoleSink → 把完整事件流写到 stdout
    # （含 assistant_text 内容 / skill_dispatched / sub-skill 输出 /
    # skill_dispatch_permission_denied 等），方便从终端直接诊断而无需翻 JSONL。
    # color=False —— uvicorn 转发后 stdout.isatty() 不可靠，避免 ANSI 乱码。
    if id(engine) not in _consoled_engines:
        _consoled_engines.add(id(engine))
        attach_console_sink(engine, color=False)
    sub_id = await engine.submit(taifeng.UserMessage(text=req.message))
    asyncio.create_task(_bridge_events(
        req.demo_id, req.session_id, engine, sub_id,
        detached=meta.streams_detached))
    return {"submission_id": sub_id, "demo_id": req.demo_id, "session_id": req.session_id}


async def _bridge_events(
    demo_id: str, session_id: str, engine: taifeng.AgentEngine, sub_id: str,
    *, detached: bool = False,
) -> None:
    """把 engine 事件流翻译成 dict 推给前端订阅者。

    **非 detached（默认）**：按 submission_id 过滤本提交事件；根 turn 终态或根
    thread 挂起即退出（见 form_hitl / code_review 等 demo）。

    **detached**（streams_detached demo）：engine 已 session 隔离，故不按
    submission_id 过滤（spawn 事件 submission_id=handle_id / barrier 事件
    =barrier_id / resume 事件=resume sub.id 都要转发）。退出谓词纯事件驱动：
    根 turn 终态 ∧ ``engine.has_live_spawns()`` 为假 ∧ 无未触发 barrier ∧
    无在跑 then_thread —— 保证 spawn 后台活动（含挂起待 HITL 的专家）与 join-barrier
    触发的 joint-consult 输出都不会被提前截断。
    """
    sub_key = f"{demo_id}:{session_id}"
    # detached 退出谓词的 bookkeeping
    root_done = False
    open_barriers: set[str] = set()        # registered 未 fired 的 barrier
    pending_then_threads: set[str] = set()  # fired 后聚合 turn 仍在跑的 then_thread
    try:
        async for ev in engine.subscribe_all():
            data = ev.msg.data if hasattr(ev.msg, "data") else {}
            if not detached:
                # ── 原有 per-submission 行为，保持不变 ──
                if ev.submission_id != sub_id:
                    continue
                payload = {"kind": ev.msg.kind, "submission_id": ev.submission_id,
                           "data": data}
                for q in _event_subs.get(sub_key, []):
                    q.put_nowait(payload)
                if ev.msg.kind in ("turn_completed", "turn_failed") and data.get(
                    "is_root", False
                ):
                    break
                if ev.msg.kind == "turn_suspended" and (
                    data.get("thread_id") == engine.thread_id
                ):
                    break
                continue

            # ── detached 分支：转发全部本 session 事件 ──
            payload = {"kind": ev.msg.kind, "submission_id": ev.submission_id,
                       "data": data}
            for q in _event_subs.get(sub_key, []):
                q.put_nowait(payload)

            # bookkeeping
            if ev.msg.kind in ("turn_completed", "turn_failed") and data.get(
                "is_root", False
            ):
                root_done = True
            elif ev.msg.kind == "join_barrier_registered":
                bid = data.get("barrier_id")
                if bid:
                    open_barriers.add(bid)
            elif ev.msg.kind == "join_barrier_fired":
                bid = data.get("barrier_id")
                if bid:
                    open_barriers.discard(bid)
                then_tid = data.get("then_thread_id")
                if then_tid:
                    pending_then_threads.add(then_tid)
            elif ev.msg.kind in ("turn_completed", "turn_failed"):
                # 聚合 turn（then_thread）跑完 → 解除其挂起标记
                pending_then_threads.discard(data.get("thread_id"))

            # 退出谓词：含 has_live_spawns()，故挂起待 HITL 的专家会让 bridge
            # 保持存活，其 resume 续跑事件经同一 bridge 回流（Task 6 不另起 bridge 的前提）。
            if (root_done and not engine.has_live_spawns()
                    and not open_barriers and not pending_then_threads):
                break
    except Exception:
        logger.exception("event bridge failed for sub_key=%s", sub_key)


@app.get("/api/events/{demo_id}/{session_id}")
async def events(demo_id: str, session_id: str) -> StreamingResponse:
    """SSE 流：按 (demo_id, session_id) 订阅 EventMsg。"""
    if demo_id not in DEMOS:
        raise HTTPException(404, f"unknown demo_id: {demo_id}")
    sub_key = f"{demo_id}:{session_id}"
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
    _event_subs.setdefault(sub_key, []).append(q)

    async def gen():
        shutdown = _get_shutdown_event()
        try:
            hello = {
                "kind": "_connected",
                "data": {"demo_id": demo_id, "session_id": session_id},
            }
            yield f"data: {json.dumps(hello)}\n\n"
            while not shutdown.is_set():
                # 同时等待「新事件」与「进程关停」——任一先到即返回；关停时立即收尾退出，
                # 不再死等 15s keepalive 被 uvicorn 强杀（消除关停期 CancelledError 噪声栈）。
                get_task = asyncio.ensure_future(q.get())
                stop_task = asyncio.ensure_future(shutdown.wait())
                try:
                    done, _ = await asyncio.wait(
                        {get_task, stop_task},
                        timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (get_task, stop_task):
                        if not t.done():
                            t.cancel()
                if stop_task in done:
                    break  # 进程关停：干净退出
                if get_task in done:
                    yield f"data: {json.dumps(get_task.result(), ensure_ascii=False)}\n\n"
                else:
                    yield ": keepalive\n\n"  # 15s 超时心跳
        except asyncio.CancelledError:
            # 客户端断开 / 强制关停：静默退出，不冒泡成 ASGI ERROR 噪声栈。
            pass
        finally:
            subs = _event_subs.get(sub_key, [])
            if q in subs:
                subs.remove(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/hitl/{request_id}")
async def hitl_decide(request_id: str, body: HitlDecisionRequest) -> dict[str, Any]:
    fut = _pending_hitl.get(request_id)
    if fut is None:
        raise HTTPException(404, f"unknown request_id: {request_id}")
    if fut.done():
        raise HTTPException(409, "already decided")
    decision = (
        PermissionDecision.allow(reason=body.reason, remember="once")
        if body.granted
        else PermissionDecision.deny(reason=body.reason, remember="once")
    )
    fut.set_result(decision)
    logger.info("HITL decided: rid=%s granted=%s", request_id, body.granted)
    return {"ok": True}


@app.post("/api/resume")
async def resume_form(req: ResumeFormRequest) -> dict[str, Any]:
    """表单 HITL 续跑：提交 Resume(thread, {request_id: payload}) 并桥接续跑事件。

    与 ``/api/hitl``（权限审批，回填 Future）不同——表单走 Resume Op：用户填写的
    ``payload`` 直接成为挂起 call 的 ``function_call_output``，子 turn 续跑 → 回传父
    call_skill → 根 turn 继续派后续子 skill。续跑事件经 ``_bridge_events`` 推回前端。
    """
    from taifeng.loop.submission import Resume

    if req.demo_id not in DEMOS:
        raise HTTPException(404, f"unknown demo_id: {req.demo_id}")
    pool = _pools.get(req.demo_id)
    if pool is None:
        # pool 必然已存在（挂起发生在先）；不存在说明状态丢失
        raise HTTPException(409, "no active pool for this demo (suspension lost?)")
    meta = DEMOS[req.demo_id]
    engine = await pool.get_or_create(
        session_id=f"{req.demo_id}:{req.session_id}",
        entry_skill_id=meta.entry_skill_id,
    )
    sub_id = await engine.submit(Resume(
        thread_id=req.thread_id,
        resolutions={req.request_id: req.payload},
    ))
    # detached demo 的 chat bridge 仍存活（has_live_spawns 含 suspended），resume
    # 续跑事件经它回流；再起一条会重复推送，故仅非 detached demo 才另起 bridge。
    if not meta.streams_detached:
        asyncio.create_task(
            _bridge_events(req.demo_id, req.session_id, engine, sub_id)
        )
    logger.info(
        "form resume: demo=%s thread=%s rid=%s keys=%s",
        req.demo_id, req.thread_id, req.request_id, list(req.payload.keys()),
    )
    return {"submission_id": sub_id, "demo_id": req.demo_id}


# ──────────────────────────────────────────────────────────────────
# 历史会话（R5 resume）—— 列出 / 读取已持久化 thread
# ──────────────────────────────────────────────────────────────────


async def _pool_or_none(demo_id: str) -> taifeng.EnginePool | None:
    """取/建 demo 的 pool；模型客户端缺失（无 API key）时返回 None 而非抛 503。

    用途：列历史 / 读历史是纯存储读取（JSONL + SQLite 索引），不依赖模型；
    未配置模型时优雅降级为「无历史」，而不是把列表接口也连带 503。
    """
    if _model_client is None:
        return None
    return await _get_or_create_pool(demo_id)


async def _thread_preview(pool: taifeng.EnginePool, thread_id: str) -> tuple[str, int]:
    """单次扫描 thread：返回 (标题, 聊天消息数)。

    标题 = 首条 user 消息文本（截断 60 字）；消息数 = user/assistant 两类之和。
    不复用 ThreadInfo.item_count —— 默认实现下其值不随 append 维护（恒 0），会误导。
    """
    title = ""
    count = 0
    async for item in await pool.store.load_thread(thread_id):
        if item.kind in ("user_message", "assistant_message"):
            count += 1
            if not title and item.kind == "user_message":
                title = str(item.payload.get("text", "")).replace("\n", " ")[:60]
    return title, count


@app.get("/api/threads/{demo_id}")
async def list_threads(demo_id: str) -> dict[str, Any]:
    """列出该 demo 已持久化的根会话 thread（resume 入口数据源）。

    只读存储，与模型无关；未配置模型时返回空列表 + available=False（前端据此提示）。
    仅返回 ``source=session:*`` 的根 thread —— 子 skill 派发出的子 thread 不作为
    独立续聊入口。按 last_activity 倒序（store.list_threads 既有序）。
    """
    if demo_id not in DEMOS:
        raise HTTPException(404, f"unknown demo_id: {demo_id}")
    pool = await _pool_or_none(demo_id)
    if pool is None:
        return {"threads": [], "available": False}
    infos = await pool.store.list_threads(limit=50)
    threads: list[dict[str, Any]] = []
    for info in infos:
        if not (info.source or "").startswith("session:"):
            continue
        title, msg_count = await _thread_preview(pool, info.thread_id)
        # 空 thread（创建后未产生任何 user/assistant 消息）不作为续接入口
        if msg_count == 0:
            continue
        threads.append({
            "thread_id": info.thread_id,
            "created_at": info.created_at.isoformat() if info.created_at else None,
            "item_count": msg_count,
            "entry_skill_id": info.entry_skill_id,
            "title": title,
        })
    return {"threads": threads, "available": True}


@app.get("/api/threads/{demo_id}/{thread_id}")
async def thread_messages(demo_id: str, thread_id: str) -> dict[str, Any]:
    """读取单个 thread 的对话历史（user / assistant 文本），供续聊前渲染到聊天区。

    只取面向聊天的两类 item（user_message / assistant_message）；function_call /
    reasoning / compacted 等内部 item 不进聊天区（它们属事件流维度）。
    """
    if demo_id not in DEMOS:
        raise HTTPException(404, f"unknown demo_id: {demo_id}")
    pool = await _pool_or_none(demo_id)
    if pool is None:
        raise HTTPException(503, "model client not initialized")
    msgs: list[dict[str, str]] = []
    async for item in await pool.store.load_thread(thread_id):
        if item.kind == "user_message":
            msgs.append({"role": "user", "text": str(item.payload.get("text", ""))})
        elif item.kind == "assistant_message":
            text = str(item.payload.get("text", ""))
            if text:
                msgs.append({"role": "assistant", "text": text})
    if not msgs:
        raise HTTPException(404, f"thread not found or empty: {thread_id}")
    return {"thread_id": thread_id, "messages": msgs}


if __name__ == "__main__":
    import contextlib
    import signal

    import uvicorn

    # timeout_graceful_shutdown=2：SSE 是长连接，Ctrl+C 时不强制超时会卡死
    # 等所有 SSE 客户端断开。给 2s 优雅窗口后强制 close，避免反复按 Ctrl+C。
    config = uvicorn.Config(
        app, host="127.0.0.1", port=8765,
        log_level="info", timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    # 自管信号：uvicorn 默认信号处理会在「优雅 drain 超时后」才跑 lifespan 收尾，
    # 那时再 set 关停事件已太晚，长连接 SSE 仍被强杀抛 CancelledError 噪声栈。
    # 这里收到 SIGINT/SIGTERM 时**先**广播 _shutdown_event（SSE 生成器立即退出），
    # **再**令 uvicorn server.should_exit —— 顺序正确，优雅窗口内连接就已干净收尾。
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _serve() -> None:
        """自管信号 + 运行 uvicorn server。"""
        loop = asyncio.get_running_loop()

        def _on_signal() -> None:
            _get_shutdown_event().set()
            server.should_exit = True

        for _sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(_sig, _on_signal)
        await server.serve()

    asyncio.run(_serve())
