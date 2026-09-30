"""真实端点验证：历史里写坏的 tool call 参数按显式标记回放（ADR 0074）。

Anthropic / Gemini 要求历史 tool call 的参数是 JSON 对象。模型当初产出的参数若不是合法 JSON 对象，
回放时用 ``{"__invalid_arguments__": ..., "__raw_arguments__": ...}`` 标记对象表达。本脚本把两种
写坏的历史（非法 JSON、非对象 JSON）发给真实端点，确认：

1. 端点接受请求（此前非对象 JSON 会原样穿透、被 400 拒绝，会话此后每次请求都失败）；
2. 模型能据此继续对话。

只对要求对象形态的 provider 有意义（anthropic / gemini）；其余 provider 直接退出。

运行：
    cd taifeng
    LLM_BOOTSTRAP_PROVIDER=gemini PYTHONPATH=src uv run python examples/real_llm/tool_args_replay_verify.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _provider_bootstrap import (  # noqa: E402
    ProviderBootstrapError,
    build_model_client,
    load_dotenv_files,
)

load_dotenv_files()

from taifeng.llm.types import ApiMessage, ApiRequest, ToolSpecRef  # noqa: E402
from taifeng.loop.cancellation import CancellationToken  # noqa: E402

_TOOL = ToolSpecRef(
    name="search",
    description="按关键词搜索资料",
    input_schema={
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    },
)

_CASES: tuple[tuple[str, str, str], ...] = (
    ("invalid_json", '{"q": "cats"', "invalid_arguments: invalid_json: 参数不是合法 JSON"),
    ("not_an_object", "[1, 2]", "invalid_arguments: not_an_object: got list"),
)


def _request(model: str, arguments: str, error: str) -> ApiRequest:
    """一段历史：模型发过一次写坏参数的调用，工具回了错误；现在请它继续。"""
    return ApiRequest(
        model=model,
        system_prompt=["你是一个简洁的助手。"],
        messages=[
            ApiMessage(role="user", content="帮我搜一下猫的资料。"),
            ApiMessage(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": arguments},
                }],
            ),
            ApiMessage(role="tool", content=error, tool_call_id="call_1", name="search"),
            ApiMessage(
                role="user",
                content="刚才那次调用出了什么问题？用一句话回答，不要再调用工具。",
            ),
        ],
        tools=[_TOOL],
        max_output_tokens=512,
    )


async def _run_case(client: object, model: str, label: str, arguments: str, error: str) -> bool:
    """发一次请求；端点接受且有产出即通过。"""
    cancel = CancellationToken(name=f"verify:{label}")
    text = ""
    completed = False
    session = client.session(cancel=cancel, model=model)  # type: ignore[attr-defined]
    async with session as s:
        async for event in s.stream(_request(model, arguments, error)):
            if event.kind == "text_delta":
                text += str(event.data.get("text", ""))
            elif event.kind == "completed":
                completed = True
    ok = completed
    print(f"  {'✅PASS' if ok else '❌FAIL'}  {label}: 端点接受请求，模型回复 {len(text)} 字")
    if text:
        print(f"         {text.strip()[:120]}")
    return ok


async def main() -> int:
    try:
        client, meta = build_model_client(retry=False)
    except ProviderBootstrapError as exc:
        print(f"❌ 无法构造客户端：{exc}")
        return 2
    provider = str(meta.get("provider"))
    model = str(meta.get("model"))
    if provider not in ("anthropic", "gemini"):
        print(f"provider={provider} 把参数当字符串回放，不需要本验证；请用 anthropic 或 gemini。")
        return 2
    print(f"provider={provider} model={model}")
    results = []
    for label, arguments, error in _CASES:
        try:
            results.append(await _run_case(client, model, label, arguments, error))
        except Exception as exc:  # noqa: BLE001  # 验证脚本：任何失败都要如实打印
            print(f"  ❌FAIL  {label}: {type(exc).__name__}: {str(exc)[:300]}")
            results.append(False)
    passed = all(results)
    print("✅ 全部通过" if passed else "❌ 有用例失败")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
