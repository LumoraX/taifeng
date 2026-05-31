"""instructions-injection 端到端示例 —— MockClient + 两层指令 + 热更 + snapshot。

运行：
    cd taifeng
    PYTHONPATH=src python examples/basic/instructions_basic.py

预期输出：
    - 第一 turn 的 system prompt 含两层 <system_instructions> 块
    - 热更 'tenant-persona' 后第二 turn 的 system prompt 含新文本
    - 通过 engine.instructions_snapshot() 看到 ResolvedInstruction 列表
    - 业务侧 fetch 失败 → InstructionFetchError → turn_failed（fail-fast）

参考：docs/decisions/0007-instructions-as-injection.md
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import taifeng
from taifeng.llm.providers.mock import MockClient, MockSession, MockTurn
from taifeng.llm.types import ApiRequest, TokenUsage

# --- 与 minimal_chat.py 同样的最小 skill 定义 -----------------------------

ATOMIC = """---
name: style-checker
description: 代码风格审查
version: 1.0.0
type: atomic
---
# 风格审查
内容。
"""

COMPOSITE = """---
name: code-reviewer
description: 代码审查专家
version: 1.0.0
type: composite
entry: true
model: mock-model
child_skills: [style-checker]
tool_names: []
max_call_depth: 3
---
# 代码审查专家
你是代码审查专家。
"""


# --- 业务侧实现的 InstructionSource ---------------------------------------


class _TenantPersonaSource:
    """业务侧动态指令：根据 ctx.metadata 中的业务键返回不同 persona 文本。

    示例：实际业务里这里走 DB / 配置中心。
    红线：fetch 内 SHALL NOT 自行发起 HITL 询问机制（spec D6）。
    """

    def __init__(self, persona_text: str) -> None:
        self._text = persona_text

    async def fetch(self, ctx) -> str | None:
        return self._text


# --- 一个能记录请求的 Mock client（便于打印 system prompt） -----------------


class _CapturingClient(MockClient):
    """记录每次 LLM 调用收到的 ApiRequest。"""

    def __init__(self, *, turns) -> None:
        super().__init__(turns=turns)
        self.captured: list[ApiRequest] = []

    def session(self, *, cancel, model=None):  # type: ignore[override]
        return _CapturingSession(super().session(cancel=cancel, model=model), self)


class _CapturingSession:
    def __init__(self, inner: MockSession, owner: _CapturingClient) -> None:
        self._inner = inner
        self._owner = owner

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)

    async def stream(self, request: ApiRequest):
        self._owner.captured.append(request)
        async for ev in self._inner.stream(request):
            yield ev


# --- 主流程 ----------------------------------------------------------------


async def _drive_turn(engine, text: str) -> str:
    sub_id = await engine.submit(taifeng.UserMessage(text=text))
    async for ev in engine.subscribe(sub_id):
        if ev.msg.kind in ("turn_completed", "turn_failed"):
            return ev.msg.kind
    return "unknown"


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skills = root / "skills"
        threads = root / "threads"
        (skills / "style-checker").mkdir(parents=True)
        (skills / "style-checker" / "SKILL.md").write_text(
            ATOMIC, encoding="utf-8",
        )
        (skills / "code-reviewer").mkdir(parents=True)
        (skills / "code-reviewer" / "SKILL.md").write_text(
            COMPOSITE, encoding="utf-8",
        )

        # MockClient 脚本：每个 turn 一条无工具调用的简单回复
        client = _CapturingClient(turns=[
            MockTurn(text="turn1 ok", usage=TokenUsage(
                input_tokens=100, output_tokens=10, total_tokens=110,
            )),
            MockTurn(text="turn2 ok", usage=TokenUsage(
                input_tokens=100, output_tokens=10, total_tokens=110,
            )),
        ])

        # 配置两层指令
        layers = [
            taifeng.InstructionLayer(
                name="global-policy",
                source="GLOBAL_POLICY: 始终用中文回答。",
                scope="engine",
                priority=10,
            ),
            taifeng.InstructionLayer(
                name="tenant-persona",
                source=_TenantPersonaSource("PERSONA: 温柔、耐心、主动追问。"),
                scope="session",
                cache_ttl_seconds=600,
                priority=50,
            ),
        ]

        pool = await taifeng.EnginePool.create(
            skills_dir=skills,
            threads_dir=threads,
            model_client=client,
            compressors=[],
            instruction_layers=layers,
        )
        engine = await pool.get_or_create(
            session_id="s1", entry_skill_id="code-reviewer",
        )

        # 第一 turn ----------------------------------------------------
        kind = await _drive_turn(engine, "你好，请审查一段代码。")
        assert kind == "turn_completed", f"expected turn_completed, got {kind}"

        sp1 = client.captured[0].system_prompt[0]
        print("=== Turn 1 system prompt ===")
        print(sp1[:800])
        print("...")
        assert "GLOBAL_POLICY" in sp1, "engine scope layer 未注入"
        assert "PERSONA: 温柔" in sp1, "session scope layer 未注入"
        assert sp1.index("GLOBAL_POLICY") < sp1.index("PERSONA"), \
            "priority 升序拼接失败"

        # 热更 persona -------------------------------------------------
        update_sub = await engine.submit(taifeng.UpdateInstructions(
            layer_name="tenant-persona",
            new_source="PERSONA_V2: 严肃、专业、直接给结论。",
        ))
        async for ev in engine.subscribe(update_sub):
            if ev.msg.kind == "instruction_updated":
                print(
                    f"\n[hot-swap] layer={ev.msg.data['layer_name']} "
                    f"new_kind={ev.msg.data['new_source_kind']}",
                )
                break

        # 第二 turn ----------------------------------------------------
        kind2 = await _drive_turn(engine, "另一个结节。")
        assert kind2 == "turn_completed"
        sp2 = client.captured[1].system_prompt[0]
        print("\n=== Turn 2 system prompt (after hot-swap) ===")
        print(sp2[:800])
        print("...")
        assert "PERSONA_V2" in sp2, "热更后新文本未生效"
        assert "PERSONA: 温柔" not in sp2, "热更后旧文本仍残留"

        # snapshot 外部读取 -------------------------------------------
        snap = engine.instructions_snapshot()
        print(f"\n=== instructions_snapshot ({len(snap)} entries) ===")
        for item in snap:
            print(
                f"  - name={item.name} scope={item.scope} "
                f"kind={item.source_kind} cache_hit={item.cache_hit} "
                f"len={len(item.text)}",
            )

        await pool.close()
        print("\n[done] all assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
