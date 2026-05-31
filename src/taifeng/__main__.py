"""Taifeng CLI 入口 —— ``python -m taifeng <command> ...``。

子命令：
    skill list <dir>            列出所有 skill（id, type, entry, child_count）
    skill show <dir> <id>       展示单个 skill 详情
    skill validate <dir>        加载 + 静态环检测，失败时 exit 1
    engine demo <dir> <entry>   交互式 demo（MockClient，回显模式）

设计：argparse 不引第三方依赖。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from taifeng.skill import (
    CircularSkillReference,
    FilesystemSkillRegistry,
    SkillLoadError,
    SkillValidationError,
)


def _cmd_skill_list(args: argparse.Namespace) -> int:
    async def run() -> int:
        try:
            registry = await FilesystemSkillRegistry.load(args.skills_dir)
        except (
            SkillValidationError,
            CircularSkillReference,
            SkillLoadError,
            FileNotFoundError,
        ) as e:
            print(f"❌ load failed: {e}", file=sys.stderr)
            return 1
        snap = registry.snapshot()
        if not snap.skills:
            print("(empty)")
            return 0
        # 按 type + entry 排序
        sorted_skills = sorted(
            snap.skills,
            key=lambda s: (not s.entry, s.type, s.id),
        )
        # 头部
        print(f"{'ID':<32}  {'TYPE':<10}  {'ENTRY':<6}  {'CHILDREN'}")
        print(f"{'-' * 32}  {'-' * 10}  {'-' * 6}  {'-' * 20}")
        for s in sorted_skills:
            entry_mark = "✓" if s.entry else ""
            children = ",".join(sorted(s.child_skills)) if s.child_skills else "-"
            if len(children) > 30:
                children = children[:27] + "..."
            print(f"{s.id:<32}  {s.type:<10}  {entry_mark:<6}  {children}")
        print()
        print(f"total {len(snap.skills)} skill(s), {len(snap.entries())} entry-eligible")
        return 0

    return asyncio.run(run())


def _cmd_skill_show(args: argparse.Namespace) -> int:
    async def run() -> int:
        registry = await FilesystemSkillRegistry.load(args.skills_dir)
        defn = registry.get(args.skill_id)
        if defn is None:
            print(f"❌ skill not found: {args.skill_id}", file=sys.stderr)
            return 1
        print(f"# {defn.id}")
        print(f"name:        {defn.name}")
        print(f"description: {defn.description}")
        print(f"version:     {defn.version}")
        print(f"type:        {defn.type}")
        print(f"entry:       {defn.entry}")
        if defn.model:
            print(f"model:       {defn.model}")
        if defn.child_skills:
            print(f"child_skills: {sorted(defn.child_skills)}")
        if defn.tool_names:
            print(f"tool_names:  {sorted(defn.tool_names)}")
        print(f"max_call_depth: {defn.max_call_depth}")
        print(f"source:      {defn.source}")
        print(f"body_path:   {defn.body_path}")
        print()
        print("--- body ---")
        print(defn.body[:1000])
        if len(defn.body) > 1000:
            print(f"... [truncated, total {len(defn.body)} chars]")
        if defn.entry:
            snap = registry.snapshot()
            reachable = snap.reachable_from(defn.id) - {defn.id}
            print()
            print("--- reachable skills (from entry) ---")
            for r in sorted(reachable):
                print(f"  - {r}")
        return 0

    return asyncio.run(run())


def _cmd_skill_validate(args: argparse.Namespace) -> int:
    async def run() -> int:
        try:
            registry = await FilesystemSkillRegistry.load(args.skills_dir)
        except FileNotFoundError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        except SkillValidationError as e:
            print(f"❌ skill validation failed: {e}", file=sys.stderr)
            return 1
        except CircularSkillReference as e:
            print(f"❌ circular reference: {e}", file=sys.stderr)
            return 1
        except SkillLoadError as e:
            print(f"❌ load error: {e}", file=sys.stderr)
            return 1
        snap = registry.snapshot()
        print(f"✓ loaded {len(snap.skills)} skill(s)")
        print(f"✓ {len(snap.entries())} entry skill(s)")
        print("✓ no cycle detected (static analysis)")
        # 列一下 reachable 集合
        for e in snap.entries():
            reachable = snap.reachable_from(e.id) - {e.id}
            print(f"  entry={e.id} reachable={len(reachable)} skills")
        return 0

    return asyncio.run(run())


def _cmd_engine_demo(args: argparse.Namespace) -> int:
    """交互式 demo：MockClient 回声 + ConsoleSink。"""
    import tempfile

    from taifeng.llm.providers import MockClient, MockTurn
    from taifeng.llm.types import TokenUsage
    from taifeng.loop.pool import EnginePool
    from taifeng.loop.submission import UserMessage
    from taifeng.telemetry import attach_console_sink

    async def run() -> int:
        with tempfile.TemporaryDirectory() as td:
            threads = Path(td) / "threads"
            client = MockClient(turns=[
                MockTurn(
                    text=f"[mock 回声] 收到消息，entry skill = {args.entry}",
                    usage=TokenUsage(input_tokens=50, output_tokens=20, total_tokens=70),
                )
            ])
            pool = await EnginePool.create(
                skills_dir=args.skills_dir,
                threads_dir=threads,
                model_client=client,
                compressors=[],
            )
            try:
                engine = await pool.get_or_create(
                    session_id="cli-demo",
                    entry_skill_id=args.entry,
                )
            except ValueError as e:
                print(f"❌ {e}", file=sys.stderr)
                await pool.close()
                return 1
            sink_task = attach_console_sink(engine, color=sys.stdout.isatty())

            user_text = args.message or "你好"
            sub_id = await engine.submit(UserMessage(text=user_text))
            async for ev in engine.subscribe(sub_id):
                if ev.msg.kind in ("turn_completed", "turn_failed"):
                    break

            await pool.close()
            await asyncio.sleep(0.05)
            sink_task.cancel()
        return 0

    return asyncio.run(run())


def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    """启动 MCP stdio server（M3 mcp-server-mode）。

    重要约束：stdout 是 JSON-RPC 协议流；所有 log / error **必须**走 stderr。
    """
    skills_dir: Path = args.skills_dir
    storage_dir: Path = args.storage
    model: str = args.model

    if not skills_dir.is_dir():
        print(
            f"error: skills_dir not found or not a directory: {skills_dir}",
            file=sys.stderr,
        )
        return 1
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create storage_dir {storage_dir}: {e}", file=sys.stderr)
        return 1

    # 业务侧自定义入口可直接 import McpStdioServer；这里走默认 LiteLLM 路径
    try:
        from taifeng import EnginePool
        from taifeng.llm.providers.litellm_provider import LiteLLMClient
        from taifeng.mcp.prompter import McpPrompter
        from taifeng.mcp.server import McpStdioServer
        from taifeng.permission.types import PermissionPolicy
        from taifeng.skill.scripts.python import PythonScriptExecutor
        from taifeng.skill.scripts.shell import ShellScriptExecutor
    except ImportError as e:
        print(f"error: missing dependency: {e}", file=sys.stderr)
        return 1

    enable_hitl: bool = bool(getattr(args, "enable_hitl", False))
    hitl_timeout: float = float(getattr(args, "hitl_timeout", 60.0))

    # CLI 默认注入 shell + python script executor，覆盖 SKILL.md 中 scripts 字段
    # 业务侧自定义入口可传更复杂的 mapping（如 custom language / sandbox）
    default_script_executors = {
        "shell": ShellScriptExecutor(),
        "python": PythonScriptExecutor(),
    }

    async def run() -> int:
        client = LiteLLMClient(model=model)
        # G1 T5: --enable-hitl 时先构造 server（McpPrompter 依赖它）+ policy
        # 再构造 pool；server 与 prompter 共享 stdin/stdout
        permission_policy: PermissionPolicy | None = None
        server: McpStdioServer
        if enable_hitl:
            # 占位 pool=None：先建空壳 server 让 prompter 持引用；pool 在下面真造
            # 改进：server.__init__ 收 pool；这里需要把 pool 注入。先建 pool。
            pass

        # 先建 pool（permission_policy 暂为 None；如需 HITL 会替换）
        # 但 PermissionPolicy 一旦构造就会被 wrapping 引用，pool 之后无法更换；
        # 因此分两路：
        if not enable_hitl:
            pool = await EnginePool.create(
                skills_dir=skills_dir,
                storage_dir=storage_dir,
                model_client=client,
                script_executors=default_script_executors,
            )
            server = McpStdioServer(pool)
        else:
            # 先建 server with dummy pool=None placeholder 不可行（server 构造需 pool）。
            # 改用：先建 pool，再建 server 拿到 server 引用造 prompter，最后
            # 把 prompter 注入到一个新的 PermissionPolicy，业务侧通过 pool 持
            # 有该 policy（pool 已构造 → 通过 pool._permission_policy 替换）。
            pool = await EnginePool.create(
                skills_dir=skills_dir,
                storage_dir=storage_dir,
                model_client=client,
                script_executors=default_script_executors,
            )
            server = McpStdioServer(pool)
            prompter = McpPrompter(server, timeout_seconds=hitl_timeout)
            permission_policy = PermissionPolicy(
                rules=[], default_mode="ask", prompter=prompter,
                prompter_timeout_seconds=hitl_timeout,
            )
            # pool 已构造完成，直接替换字段（demo 路径；生产建议在 create 时传入）
            pool._permission_policy = permission_policy
        try:
            await server.run()
        finally:
            await pool.close()
        return 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="taifeng",
        description="泰逢 (Taifeng) LLM Agent 微内核 CLI",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # skill 子组
    skill = sub.add_parser("skill", help="Skill 管理")
    skill_sub = skill.add_subparsers(dest="skill_cmd", required=True)

    p_list = skill_sub.add_parser("list", help="列出所有 skill")
    p_list.add_argument("skills_dir", type=Path)
    p_list.set_defaults(func=_cmd_skill_list)

    p_show = skill_sub.add_parser("show", help="展示 skill 详情")
    p_show.add_argument("skills_dir", type=Path)
    p_show.add_argument("skill_id")
    p_show.set_defaults(func=_cmd_skill_show)

    p_validate = skill_sub.add_parser("validate", help="加载 + 静态环检测")
    p_validate.add_argument("skills_dir", type=Path)
    p_validate.set_defaults(func=_cmd_skill_validate)

    # engine 子组
    engine = sub.add_parser("engine", help="Engine demo")
    engine_sub = engine.add_subparsers(dest="engine_cmd", required=True)

    p_demo = engine_sub.add_parser("demo", help="MockClient 交互演示")
    p_demo.add_argument("skills_dir", type=Path)
    p_demo.add_argument("entry", help="entry skill id")
    p_demo.add_argument("--message", "-m", help="发给 engine 的消息", default=None)
    p_demo.set_defaults(func=_cmd_engine_demo)

    # mcp 子组（M3 mcp-server-mode）
    mcp = sub.add_parser("mcp", help="MCP server / client 操作")
    mcp_sub = mcp.add_subparsers(dest="mcp_cmd", required=True)

    p_serve = mcp_sub.add_parser(
        "serve",
        help="把 taifeng 暴露为 MCP stdio server",
    )
    p_serve.add_argument("skills_dir", type=Path, help="SKILL.md 根目录")
    p_serve.add_argument(
        "--storage", required=True, type=Path,
        help="JSONL 持久化目录（EnginePool storage_dir）",
    )
    p_serve.add_argument(
        "--model", default="gpt-4o-mini",
        help="LiteLLM model 名（默认 gpt-4o-mini）",
    )
    p_serve.add_argument(
        "--enable-hitl", action="store_true",
        help=(
            "启用 HITL（demo）：注入 McpPrompter + "
            "PermissionPolicy(default_mode='ask')。"
            "client 必须支持 MCP elicitation/create 协议。"
        ),
    )
    p_serve.add_argument(
        "--hitl-timeout", type=float, default=60.0,
        help="HITL elicitation 超时秒数（默认 60.0；仅 --enable-hitl 生效）",
    )
    p_serve.set_defaults(func=_cmd_mcp_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
